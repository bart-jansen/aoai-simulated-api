import asyncio
from dataclasses import dataclass
import logging
import secrets
import time
import traceback
from typing import Annotated, Callable
from fastapi import Depends, FastAPI, Request, Response, HTTPException, status
from fastapi.security import APIKeyHeader
from limits import storage
from opentelemetry import trace, metrics

from aoai_simulated_api import constants
from aoai_simulated_api.generator.manager import invoke_generators
from aoai_simulated_api.limiters import create_openai_limiter, create_doc_intelligence_limiter
from aoai_simulated_api.models import Config, RequestContext
from aoai_simulated_api.record_replay.handler import RecordReplayHandler
from aoai_simulated_api.record_replay.persistence import YamlRecordingPersister


@dataclass
class SimulatorMetrics:
    histogram_latency_base: metrics.Histogram
    histogram_latency_full: metrics.Histogram
    histogram_tokens_used: metrics.Histogram
    histogram_tokens_requested: metrics.Histogram


def _get_simulator_metrics() -> SimulatorMetrics:
    meter = metrics.get_meter(__name__)
    return SimulatorMetrics(
        histogram_latency_base=meter.create_histogram(
            name="aoai-simulator.latency.base",
            description="Latency of handling the request (before adding simulated latency)",
            unit="seconds",
        ),
        histogram_latency_full=meter.create_histogram(
            name="aoai-simulator.latency.full",
            description="Full latency of handling the request (including simulated latency)",
            unit="seconds",
        ),
        histogram_tokens_used=meter.create_histogram(
            name="aoai-simulator.tokens_used",
            description="Number of tokens used per request",
            unit="tokens",
        ),
        histogram_tokens_requested=meter.create_histogram(
            name="aoai-simulator.tokens_requested",
            description="Number of tokens across all requests (success or not)",
            unit="tokens",
        ),
    )


def get_simulator(logger: logging.Logger, config: Config) -> FastAPI:
    """
    Create the FastAPI app for the simulator based on provided configuration
    """
    app = FastAPI()

    simulator_metrics = _get_simulator_metrics()

    # api-key header for OpenAI
    # ocp-apim-subscription-key header for doc intelligence
    api_auth_header_scheme = APIKeyHeader(name="Authorization", auto_error=False)
    api_key_header_scheme = APIKeyHeader(name="api-key", auto_error=False)
    ocp_apim_subscription_key_header_scheme = APIKeyHeader(name="ocp-apim-subscription-key", auto_error=False)

    def validate_api_key(
        api_auth_header_key: Annotated[str, Depends(api_auth_header_scheme)],
        api_key: Annotated[str, Depends(api_key_header_scheme)],
        ocp_apim_subscription_key: Annotated[str, Depends(ocp_apim_subscription_key_header_scheme)],
    ):
        # TODO: check if api_auth header bearer is valid
        if api_auth_header_key:
            logger.info("🔑 API Key for TWIN API provided")
            return True
        if api_key and secrets.compare_digest(api_key, config.simulator_api_key):
            return True
        if ocp_apim_subscription_key and secrets.compare_digest(ocp_apim_subscription_key, config.simulator_api_key):
            return True

        logger.warning("🔒 Missing or incorrect API Key provided")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or incorrect API Key",
        )

    logger.info("🚀 Starting aoai-simulated-api in %s mode", config.simulator_mode)
    logger.info("🗝️ Simulator api-key        : %s", config.simulator_api_key)

    if config.simulator_mode in ["record", "replay"]:
        logger.info("📼 Recording directory      : %s", config.recording.dir)
        logger.info("📼 Recording auto-save      : %s", config.recording.autosave)
        persister = YamlRecordingPersister(config.recording.dir)

        record_replay_handler = RecordReplayHandler(
            simulator_mode=config.simulator_mode,
            persister=persister,
            forwarders=config.recording.forwarders,
            autosave=config.recording.autosave,
        )

    @app.get("/")
    async def root():
        return {"message": "👋 aoai-simulated-api is running"}

    @app.post("/++/save-recordings")
    def save_recordings(_: Annotated[bool, Depends(validate_api_key)]):
        if config.simulator_mode == "record":
            logger.info("📼 Saving recordings...")
            record_replay_handler.save_recordings()
            logger.info("📼 Recordings saved")
            return Response(content="📼 Recordings saved", status_code=200)

        logger.warn("⚠️ Not saving recordings as not in record mode")
        return Response(content="⚠️ Not saving recordings as not in record mode", status_code=400)

    memory_storage = storage.MemoryStorage()

    logger.info("📝 Using Doc Intelligence RPS: %s", config.doc_intelligence_rps)
    logger.info("📝 Using OpenAI deployments: %s", config.openai_deployments)

    openai_deployment_limits = (
        {name: deployment.tokens_per_minute for name, deployment in config.openai_deployments.items()}
        if config.openai_deployments
        else {}
    )
    # Dictionary of limiters keyed by name
    # Each limiter is a function that takes a response and returns a boolean indicating
    # whether the request should be allowed
    # Limiter returns Response object if request should be blocked or None otherwise
    limiters: dict[str, Callable[[RequestContext, Response], Response | None]] = {
        "openai": create_openai_limiter(memory_storage, openai_deployment_limits),
        "docintelligence": create_doc_intelligence_limiter(
            memory_storage, requests_per_second=config.doc_intelligence_rps
        ),
    }

    @app.api_route("/{full_path:path}", methods=["GET", "POST", "PUT", "DELETE"])
    async def catchall(request: Request, _: Annotated[bool, Depends(validate_api_key)]):
        logger.debug("⚡ handling route: %s", request.url.path)
        # TODO check for traceparent in inbound request and propagate
        #      to allow for correlating load test with back-end data

        start_time = time.perf_counter()  # N.B. this doesn't accound for the validate_api_key time

        try:
            response = None
            context = RequestContext(config=config, request=request)

            # Get response
            if config.simulator_mode == "generate":
                response = await invoke_generators(context, config.generators)
            elif config.simulator_mode in ["record", "replay"]:
                response = await record_replay_handler.handle_request(context)

            if not response:
                logger.error("No response generated for request: %s", request.url.path)
                return Response(status_code=500)

            # Apply limits here so that that they apply to record/replay as well as generate
            limiter_name = context.values.get(constants.SIMULATOR_KEY_LIMITER)
            limiter = limiters.get(limiter_name) if limiter_name else None
            if limiter:
                limit_response = limiter(context, response)
                if limit_response:
                    # replace response with limited response
                    response = limit_response
            else:
                logger.debug("No limiter found for response: %s", request.url.path)

            # Add latency to successful responses
            base_end_time = time.perf_counter()
            base_duration_s = base_end_time - start_time
            if response.status_code < 300:
                # TODO - apply latency to generated responses and allow config overrides
                recorded_duration_ms = context.values.get(constants.RECORDED_DURATION_MS, 0)
                recorded_duration_s = recorded_duration_ms / 1000
                extra_latency = recorded_duration_s - base_duration_s
                if extra_latency > 0:
                    current_span = trace.get_current_span()
                    current_span.set_attribute("simulator.added_latency", extra_latency)
                    await asyncio.sleep(extra_latency)

            status_code = response.status_code
            deployment_name = context.values.get(constants.SIMULATOR_KEY_DEPLOYMENT_NAME)
            tokens_used = context.values.get(constants.SIMULATOR_KEY_OPENAI_TOKENS)

            full_end_time = time.perf_counter()
            simulator_metrics.histogram_latency_base.record(
                base_duration_s,
                attributes={
                    "status_code": status_code,
                    "deployment": deployment_name,
                },
            )
            simulator_metrics.histogram_latency_full.record(
                (full_end_time - start_time),
                attributes={
                    "status_code": status_code,
                    "deployment": deployment_name,
                },
            )
            if tokens_used:
                simulator_metrics.histogram_tokens_requested.record(
                    tokens_used, attributes={"deployment": deployment_name}
                )
                if status_code < 300:
                    # only track tokens for successful requests
                    simulator_metrics.histogram_tokens_used.record(
                        tokens_used, attributes={"deployment": deployment_name}
                    )

            return response
        # pylint: disable-next=broad-exception-caught
        except Exception as e:
            logger.error("Error: %s\n%s", e, traceback.format_exc())
            return Response(status_code=500)

    return app
