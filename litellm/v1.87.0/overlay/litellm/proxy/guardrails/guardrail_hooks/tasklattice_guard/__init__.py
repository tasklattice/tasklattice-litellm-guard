from typing import TYPE_CHECKING

from .tasklattice_guard import TaskLatticeGuard

if TYPE_CHECKING:
    from litellm.types.guardrails import Guardrail, LitellmParams


def initialize_guardrail(litellm_params: "LitellmParams", guardrail: "Guardrail"):
    import litellm

    callback = TaskLatticeGuard(
        api_base=litellm_params.api_base,
        credential_name=getattr(litellm_params, "credential_name", None),
        guardrail_name=guardrail.get("guardrail_name", "TaskLattice Guard"),
        event_hook=litellm_params.mode,
        default_on=getattr(litellm_params, "default_on", None),
        unreachable_fallback=getattr(
            litellm_params, "unreachable_fallback", "fail_closed"
        ),
        timeout_seconds=getattr(litellm_params, "timeout_seconds", 10),
    )
    litellm.logging_callback_manager.add_litellm_callback(callback)
    return callback


guardrail_initializer_registry = {"tasklattice_guard": initialize_guardrail}
guardrail_class_registry = {"tasklattice_guard": TaskLatticeGuard}
