import asyncio
import json
import logging
from flask import Blueprint, request, url_for, jsonify, make_response, Response

from meltano.core.job import JobFinder, State
from meltano.core.behavior.canonical import Canonical
from meltano.core.plugin import PluginRef, Profile
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
from meltano.core.schedule_service import (
    ScheduleService,
    ScheduleAlreadyExistsError,
    ScheduleDoesNotExistError,
)
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
                "code": f"A pipeline with the name '{ex.schedule.name}' already exists. Try renaming the pipeline.",
            }
        ),
        409,
    )


@orchestrationsBP.errorhandler(ScheduleDoesNotExistError)
def _handle(ex):
    return (
        jsonify(
            {
                "error": True,
                "code": f"A pipeline with the name '{ex.name}' does not exist..",
            }
        ),
        404,
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
    Endpoint for getting a plugin's configuration profiles
    """

    project = Project.find()
    settings = PluginSettingsService(project)
    plugin = ConfigService(project).get_plugin(plugin_ref)

    profiles = settings.as_profile_configs(db.session, plugin, redacted=True)

    # freeze the `config` keys
    for profile in profiles:
        if not "config" in profile:
            continue

        profile["config"] = freeze_keys(profile["config"])

    return jsonify(
        {
            # freeze the keys because they are used for lookups
            "profiles": profiles,
            "settings": Canonical.as_canonical(
                settings.get_definition(plugin).settings
            ),
        }
    )


@orchestrationsBP.route(
    "/<plugin_ref:plugin_ref>/configuration/profiles", methods=["POST"]
)
def add_plugin_configuration_profile(plugin_ref) -> Response:
    """
    Endpoint for adding a configuration profile to a plugin
    """
    payload = request.get_json()
    project = Project.find()
    config = ConfigService(project)
    plugin = config.get_plugin(plugin_ref)
    settings = PluginSettingsService(project)

    # create the new profile for this plugin
    profile = plugin.add_profile(
        slugify(payload["name"]), config=payload["config"], label=payload["name"]
    )

    config.update_plugin(plugin)
    plugin.use_profile(profile)

    # load the default config for the profile
    profile.config = freeze_keys(settings.as_config(db.session, plugin, redacted=True))

    return jsonify(profile.canonical())


@orchestrationsBP.route("/<plugin_ref:plugin_ref>/configuration", methods=["PUT"])
def save_plugin_configuration(plugin_ref) -> Response:
    """
    Endpoint for persisting a plugin configuration
    """
    project = Project.find()
    payload = request.get_json()
    plugin = ConfigService(project).get_plugin(plugin_ref)

    # TODO iterate pipelines and save each, also set this connector's profile (reuse `pipelineInFocusIndex`?)

    settings = PluginSettingsService(project)

    for profile in payload:
        # select the correct profile
        plugin.use_profile(plugin.get_profile(profile["name"]))

        for name, value in profile["config"].items():
            # we want to prevent the edition of protected settings from the UI
            if settings.find_setting(plugin, name).protected:
                logging.warning("Cannot set a 'protected' configuration externally.")
                continue

            if value == "":
                settings.unset(db.session, plugin, name)
            else:
                settings.set(db.session, plugin, name, value)

    profiles = settings.as_profile_configs(db.session, plugin, redacted=True)

    # freeze the `config` keys
    for profile in profiles:
        profile["config"] = freeze_keys(profile["config"])

    return jsonify(profiles)


@orchestrationsBP.route("/<plugin_ref:plugin_ref>/configuration/test", methods=["POST"])
def test_plugin_configuration(plugin_ref) -> Response:
    """
    Endpoint for testing a plugin configuration's valid connection
    """
    project = Project.find()
    payload = request.get_json()
    config_service = ConfigService(project)
    plugin = config_service.get_plugin(plugin_ref)

    # load the correct profile
    plugin.use_profile(plugin.get_profile(payload.get("profile")))

    async def test_stream(tap_stream) -> bool:
        while not tap_stream.at_eof():
            message = await tap_stream.readline()
            json_dict = json.loads(message)
            if json_dict["type"] == "RECORD":
                return True

        return False

    async def test_extractor(config={}):
        try:
            invoker = invoker_factory(project, plugin, prepare_with_session=db.session)
            # overlay the config on top of the loaded configuration
            invoker.plugin_config = {
                **invoker.plugin_config,
                **PluginSettingsService.unredact(config),  # remove all redacted values
            }

            invoker.prepare(db.session)
            process = await invoker.invoke_async(stdout=asyncio.subprocess.PIPE)
            return await test_stream(process.stdout)
        except Exception as err:
            # if anything happens, this is not successful
            return False
        finally:
            try:
                if process:
                    psutil.Process(process.pid).terminate()
            except Exception as err:
                logging.debug(err)

    loop = asyncio.get_event_loop()
    success = loop.run_until_complete(test_extractor(payload.get("config")))

    return jsonify({"is_success": success}), 200


@orchestrationsBP.route("/pipeline_schedules", methods=["GET"])
def get_pipeline_schedules():
    """
    Endpoint for getting the pipeline schedules
    """
    project = Project.find()
    schedule_service = ScheduleService(project)
    schedules = list(map(dict, schedule_service.schedules()))
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
    Endpoint for persisting a pipeline schedule
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

    schedule = schedule_service.add(
        db.session, name, extractor, loader, transform, interval
    )
    return jsonify(dict(schedule)), 201


@orchestrationsBP.route("/pipeline_schedules", methods=["DELETE"])
def delete_pipeline_schedule() -> Response:
    """
    endpoint for deleting a pipeline schedule
    """
    incoming = request.get_json()
    name = incoming["name"]

    project = Project.find()
    schedule_service = ScheduleService(project)

    schedule_service.remove(name)
    return jsonify(name), 201
