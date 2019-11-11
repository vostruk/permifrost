import asyncio
import json
import logging

from flask import Blueprint, request, url_for, jsonify, make_response, Response

from meltano.core.job import JobFinder, State
from meltano.core.plugin import PluginRef
from meltano.core.plugin.error import PluginExecutionError, PluginLacksCapabilityError
from meltano.core.plugin.settings_service import (
    PluginSettingsService,
    PluginSettingValueSource,
)
from meltano.core.plugin_discovery_service import PluginDiscoveryService
from meltano.core.plugin_invoker import invoker_factory
from meltano.core.plugin_install_service import PluginInstallService
from meltano.core.project import Project
from meltano.core.project_add_service import ProjectAddService
from meltano.core.config_service import ConfigService
from meltano.core.schedule_service import ScheduleService, ScheduleAlreadyExistsError
from meltano.core.utils import flatten, iso8601_datetime, slugify
from meltano.core.logging import JobLoggingService, MissingJobLogException
from meltano.cli.add import extractor
from meltano.api.models import db
from meltano.api.json import freeze_keys

from meltano.api.executor import run_elt


orchestrationsBP = Blueprint(
    "orchestrations", __name__, url_prefix="/api/v1/orchestrations"
)


@orchestrationsBP.errorhandler(ScheduleAlreadyExistsError)
def _handle(ex):
    return (
        jsonify(
            {
                "error": True,
                "code": f"A schedule with the name '{ex.schedule.name}' already exists. Try renaming the schedule.",
            }
        ),
        409,
    )


@orchestrationsBP.errorhandler(MissingJobLogException)
def _handle(ex):
    return (jsonify({"error": False, "code": str(ex)}), 204)


@orchestrationsBP.route("/jobs/state", methods=["POST"])
def job_state() -> Response:
    """
    Endpoint for getting the status of N jobs
    """
    project = Project.find()
    poll_payload = request.get_json()
    job_ids = poll_payload["job_ids"]

    jobs = []
    for job_id in job_ids:
        finder = JobFinder(job_id)
        state_job = finder.latest(db.session)
        # Validate existence first as a job may not be queued yet as a result of
        # another prerequisite async process (dbt installation for example)
        if state_job:
            jobs.append(
                {
                    "job_id": job_id,
                    "is_complete": state_job.is_complete(),
                    "has_error": state_job.has_error(),
                }
            )

    return jsonify({"jobs": jobs})


@orchestrationsBP.route("/jobs/<job_id>/log", methods=["GET"])
def job_log(job_id) -> Response:
    """
    Endpoint for getting the most recent log generated by a job with job_id
    """
    project = Project.find()
    log_service = JobLoggingService(project)
    log = log_service.get_latest_log(job_id)

    finder = JobFinder(job_id)
    state_job = finder.latest(db.session)

    return jsonify(
        {
            "job_id": job_id,
            "log": log,
            "has_error": state_job.has_error() if state_job else False,
        }
    )


@orchestrationsBP.route("/run", methods=["POST"])
def run():
    project = Project.find()
    schedule_payload = request.get_json()
    job_id = run_elt(project, schedule_payload)

    return jsonify({"job_id": job_id}), 202


@orchestrationsBP.route("/<plugin_ref:plugin_ref>/configuration", methods=["GET"])
def get_plugin_configuration(plugin_ref) -> Response:
    """
    endpoint for getting a plugin's configuration
    """
    project = Project.find()
    settings = PluginSettingsService(project)
    config = flatten(
        settings.as_config(db.session, plugin_ref, redacted=True), reducer="dot"
    )

    return jsonify(
        {
            # freeze the keys because they are used for lookups
            "config": freeze_keys(config),
            "settings": settings.get_definition(plugin_ref).settings,
        }
    )


@orchestrationsBP.route("/<plugin_ref:plugin_ref>/configuration", methods=["PUT"])
def save_plugin_configuration(plugin_ref) -> Response:
    """
    endpoint for persisting a plugin configuration
    """
    project = Project.find()
    payload = request.get_json()

    settings = PluginSettingsService(project)
    for name, value in payload.items():
        # we want to prevent the edition of protected settings from the UI
        if settings.find_setting(plugin_ref, name).get("protected"):
            logging.warning("Cannot set a 'protected' configuration externally.")
            continue

        if value == "":
            settings.unset(db.session, plugin_ref, name)
        else:
            settings.set(db.session, plugin_ref, name, value)

    return jsonify(settings.as_config(db.session, plugin_ref, redacted=True))


@orchestrationsBP.route("/<plugin_ref:plugin_ref>/configuration/test", methods=["POST"])
def test_plugin_configuration(plugin_ref) -> Response:
    """
    endpoint for testing a plugin configuration's valid connection
    """
    project = Project.find()
    payload = request.get_json()
    config_service = ConfigService(project)
    plugin = config_service.find_plugin(plugin_ref.name, plugin_ref.type)
    success = False

    async def print_stream(tap_stream) -> bool:
        while not tap_stream.at_eof():
            message = await tap_stream.readline()
            json_dict = json.loads(message)
            resp = json_dict["type"] == "RECORD"
            if resp:
                return True
        return False

    async def test_extractor(config={}):
        try:
            invoker = invoker_factory(project, plugin, prepare_with_session=db.session)
            invoker.plugin_config = config
            invoker.prepare(db.session)
            process = await invoker.invoke_async(stdout=asyncio.subprocess.PIPE)
            success = await print_stream(process.stdout)
            process.kill()
        except Exception as err:
            logging.exception(err)
            success = False

    loop = asyncio.get_event_loop()
    loop.run_until_complete(test_extractor(payload))

    if success:
        return jsonify({"success": success}), 202
    else:
        return jsonify({"success": success}), 404


@orchestrationsBP.route("/pipeline_schedules", methods=["GET"])
def get_pipeline_schedules():
    """
    endpoint for getting the pipeline schedules
    """
    project = Project.find()
    schedule_service = ScheduleService(project)
    schedules = [s._asdict() for s in schedule_service.schedules()]
    for schedule in schedules:
        finder = JobFinder(schedule["name"])
        state_job = finder.latest(db.session)
        schedule["has_error"] = state_job.has_error() if state_job else False
        schedule["is_running"] = state_job.is_running() if state_job else False
        schedule["job_id"] = state_job.job_id if state_job else None

    return jsonify(schedules)


@orchestrationsBP.route("/pipeline_schedules", methods=["POST"])
def save_pipeline_schedule() -> Response:
    """
    endpoint for persisting a pipeline schedule
    """
    incoming = request.get_json()
    # Airflow requires alphanumeric characters, dashes, dots and underscores exclusively
    name = slugify(incoming["name"])
    extractor = incoming["extractor"]
    loader = incoming["loader"]
    transform = incoming["transform"]
    interval = incoming["interval"]

    project = Project.find()
    schedule_service = ScheduleService(project)

    try:
        schedule = schedule_service.add(
            db.session, name, extractor, loader, transform, interval
        )
        return jsonify(schedule._asdict()), 201
    except ScheduleAlreadyExistsError as e:
        raise ScheduleAlreadyExistsError(e.schedule)
