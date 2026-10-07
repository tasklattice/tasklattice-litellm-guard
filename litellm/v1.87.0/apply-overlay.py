#!/usr/bin/env python3
"""Apply the reviewed TaskLattice overlay to LiteLLM 1.87.0.

Every source mutation is anchored to the exact upstream text reviewed for this
version. A missing or repeated anchor aborts the image build instead of
silently producing a partially patched, fail-open image.
"""

from __future__ import annotations

import argparse
import importlib.metadata
import os
import shutil
from pathlib import Path

EXPECTED_VERSION = "1.87.0"


def replace_once(path: Path, old: str, new: str, label: str) -> None:
    source = path.read_text(encoding="utf-8")
    count = source.count(old)
    if count != 1:
        raise RuntimeError(
            f"{label}: expected one reviewed anchor in {path}, found {count}"
        )
    path.write_text(source.replace(old, new, 1), encoding="utf-8")


def replace_in_section(
    path: Path,
    section_start: str,
    section_end: str,
    old: str,
    new: str,
    label: str,
) -> None:
    source = path.read_text(encoding="utf-8")
    start = source.find(section_start)
    if start < 0:
        raise RuntimeError(f"{label}: section start missing in {path}")
    end = source.find(section_end, start + len(section_start))
    if end < 0:
        raise RuntimeError(f"{label}: section end missing in {path}")
    section = source[start:end]
    count = section.count(old)
    if count != 1:
        raise RuntimeError(
            f"{label}: expected one reviewed anchor in section, found {count}"
        )
    section = section.replace(old, new, 1)
    path.write_text(source[:start] + section + source[end:], encoding="utf-8")


def copy_overlay(version_dir: Path, app_root: Path) -> None:
    overlay = version_dir / "overlay"
    for source in sorted(overlay.rglob("*")):
        if not source.is_file():
            continue
        destination = app_root / source.relative_to(overlay)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)


def patch_backend(app_root: Path) -> None:
    endpoints = app_root / "litellm/proxy/guardrails/guardrail_endpoints.py"
    registry = app_root / "litellm/proxy/guardrails/guardrail_registry.py"

    replace_once(
        registry,
        "guardrail_initializer_registry.update(_discovered_initializers)\n",
        """guardrail_initializer_registry.update(_discovered_initializers)

# The TaskLattice overlay is an intentional part of this LiteLLM distribution.
# Register it explicitly as well as through hook discovery so application import
# order cannot make the branded Provider disappear from the Admin UI.
from litellm.proxy.guardrails.guardrail_hooks.tasklattice_guard import (
    guardrail_class_registry as tasklattice_guard_classes,
    guardrail_initializer_registry as tasklattice_guard_initializers,
)

guardrail_class_registry.update(tasklattice_guard_classes)
guardrail_initializer_registry.update(tasklattice_guard_initializers)
""",
        "explicit TaskLattice provider registration",
    )

    replace_in_section(
        endpoints,
        "async def create_guardrail(\n",
        "\n\nclass UpdateGuardrailRequest",
        '    if prisma_client is None:\n        raise HTTPException(status_code=500, detail="Prisma client not initialized")\n\n    try:\n',
        '    if prisma_client is None:\n        raise HTTPException(status_code=500, detail="Prisma client not initialized")\n\n    if (request.guardrail.get("litellm_params") or {}).get("guardrail") == "tasklattice_guard":\n        raise HTTPException(\n            status_code=400,\n            detail="Use POST /guardrails/tasklattice for TaskLattice Guard",\n        )\n\n    try:\n',
        "block generic TaskLattice creation",
    )
    replace_in_section(
        endpoints,
        "async def update_guardrail(\n",
        "\n\n@router.delete(\n",
        "        if existing_guardrail is None:\n            raise HTTPException(\n                status_code=404, detail=f\"Guardrail with ID {guardrail_id} not found\"\n            )\n\n        result = await GUARDRAIL_REGISTRY.update_guardrail_in_db(\n",
        "        if existing_guardrail is None:\n            raise HTTPException(\n                status_code=404, detail=f\"Guardrail with ID {guardrail_id} not found\"\n            )\n        existing_params = existing_guardrail.get(\"litellm_params\") or {}\n        if getattr(existing_params, \"guardrail\", None) == \"tasklattice_guard\" or (\n            isinstance(existing_params, dict)\n            and existing_params.get(\"guardrail\") == \"tasklattice_guard\"\n        ):\n            raise HTTPException(\n                status_code=400,\n                detail=\"Use PATCH /guardrails/tasklattice/{id} for TaskLattice Guard\",\n            )\n\n        result = await GUARDRAIL_REGISTRY.update_guardrail_in_db(\n",
        "block generic TaskLattice replace",
    )
    replace_in_section(
        endpoints,
        "async def delete_guardrail(\n",
        "\n\n# --- Team guardrail registration",
        "        if existing_guardrail is None:\n            raise HTTPException(\n                status_code=404, detail=f\"Guardrail with ID {guardrail_id} not found\"\n            )\n\n        result = await GUARDRAIL_REGISTRY.delete_guardrail_from_db(\n",
        "        if existing_guardrail is None:\n            raise HTTPException(\n                status_code=404, detail=f\"Guardrail with ID {guardrail_id} not found\"\n            )\n        existing_params = existing_guardrail.get(\"litellm_params\") or {}\n        if getattr(existing_params, \"guardrail\", None) == \"tasklattice_guard\" or (\n            isinstance(existing_params, dict)\n            and existing_params.get(\"guardrail\") == \"tasklattice_guard\"\n        ):\n            from litellm.proxy.guardrails.guardrail_hooks.tasklattice_guard.service import (\n                delete_tasklattice_guard_connection,\n            )\n\n            await delete_tasklattice_guard_connection(\n                prisma_client=prisma_client, guardrail_id=guardrail_id\n            )\n            return {\"message\": f\"Guardrail {guardrail_id} deleted successfully\"}\n\n        result = await GUARDRAIL_REGISTRY.delete_guardrail_from_db(\n",
        "TaskLattice credential cleanup on standard delete",
    )
    replace_in_section(
        endpoints,
        "async def patch_guardrail(\n",
        '\n\n@router.get(\n    "/guardrails/{guardrail_id}",',
        "        if existing_guardrail is None:\n            raise HTTPException(\n                status_code=404, detail=f\"Guardrail with ID {guardrail_id} not found\"\n            )\n\n        # Create updated guardrail object\n",
        "        if existing_guardrail is None:\n            raise HTTPException(\n                status_code=404, detail=f\"Guardrail with ID {guardrail_id} not found\"\n            )\n        existing_params = existing_guardrail.get(\"litellm_params\") or {}\n        if getattr(existing_params, \"guardrail\", None) == \"tasklattice_guard\" or (\n            isinstance(existing_params, dict)\n            and existing_params.get(\"guardrail\") == \"tasklattice_guard\"\n        ):\n            raise HTTPException(\n                status_code=400,\n                detail=\"Use PATCH /guardrails/tasklattice/{id} for TaskLattice Guard\",\n            )\n\n        # Create updated guardrail object\n",
        "block generic TaskLattice patch",
    )
    replace_once(
        endpoints,
        "# Usage (dashboard) endpoints: overview, detail, logs\nrouter.include_router(guardrails_usage_router)\n",
        "# TaskLattice's dedicated lifecycle keeps credentials out of GuardrailsTable.\nfrom litellm.proxy.guardrails.guardrail_hooks.tasklattice_guard.endpoints import (\n    router as tasklattice_guard_router,\n)\n\nrouter.include_router(tasklattice_guard_router)\n\n# Usage (dashboard) endpoints: overview, detail, logs\nrouter.include_router(guardrails_usage_router)\n",
        "register dedicated TaskLattice endpoints",
    )


def patch_ui(app_root: Path) -> None:
    ui_root = app_root / "ui/litellm-dashboard/src/components"
    helpers = ui_root / "guardrails/guardrail_info_helpers.tsx"
    networking = ui_root / "networking.tsx"
    add_form = ui_root / "guardrails/add_guardrail_form.tsx"
    info_view = ui_root / "guardrails/guardrail_info.tsx"
    provider_fields = ui_root / "guardrails/guardrail_provider_fields.tsx"
    guardrail_table = ui_root / "guardrails/guardrail_table.tsx"
    garden_data = ui_root / "guardrails/guardrail_garden_data.ts"
    garden_configs = ui_root / "guardrails/guardrail_garden_configs.ts"

    replace_once(
        helpers,
        'export const guardrailLogoMap: Record<string, string> = {\n',
        'export const guardrailLogoMap: Record<string, string> = {\n  "TaskLattice Guard": `${asset_logos_folder}tasklattice_guard.svg`,\n',
        "TaskLattice provider logo",
    )
    replace_once(
        helpers,
        '''export const getGuardrailLogoAndName = (guardrailValue: string): { logo: string; displayName: string } => {
''',
        '''export const formatGuardrailModes = (mode: unknown): string => {
  const modes = Array.isArray(mode) ? mode : mode ? [mode] : [];
  return modes.map(String).join(", ") || "-";
};

export const getGuardrailLogoAndName = (guardrailValue: string): { logo: string; displayName: string } => {
''',
        "shared Guardrail mode formatter",
    )

    replace_once(
        garden_data,
        '''  {
    id: "xecguard",
    name: "XecGuard",
    description:
      "CyCraft XecGuard AI security gateway. Multi-policy scanning (prompt injection, harmful content, PII, system-prompt enforcement) plus RAG context grounding.",
    category: "partner",
    logo: `${ASSET_PREFIX}xecguard.svg`,
    tags: ["Security", "Policy", "Grounding", "RAG"],
    providerKey: "Xecguard",
  },
];
''',
        '''  {
    id: "xecguard",
    name: "XecGuard",
    description:
      "CyCraft XecGuard AI security gateway. Multi-policy scanning (prompt injection, harmful content, PII, system-prompt enforcement) plus RAG context grounding.",
    category: "partner",
    logo: `${ASSET_PREFIX}xecguard.svg`,
    tags: ["Security", "Policy", "Grounding", "RAG"],
    providerKey: "Xecguard",
  },
  {
    id: "tasklattice_guard",
    name: "TaskLattice Guard",
    description:
      "Apply governed, versioned AI safety policies to LiteLLM model input and output with explicit outage behavior.",
    category: "partner",
    logo: `${ASSET_PREFIX}tasklattice_guard.svg`,
    tags: ["Input & Output", "Configurable Fallback", "Multi-Gateway"],
    providerKey: "TasklatticeGuard",
  },
];
''',
        "TaskLattice Guardrail Garden card",
    )
    replace_once(
        garden_configs,
        "  mode: string;\n",
        "  mode: string | string[];\n",
        "Guardrail preset multi-mode support",
    )
    replace_once(
        garden_configs,
        '''  // ── Partner Guardrails ──
  presidio: {
''',
        '''  // ── Partner Guardrails ──
  tasklattice_guard: {
    provider: "TasklatticeGuard",
    guardrailNameSuggestion: "TaskLattice Guard",
    mode: ["pre_call", "post_call"],
    defaultOn: true,
  },
  presidio: {
''',
        "TaskLattice Guardrail Garden preset",
    )

    networking_anchor = '''export const createGuardrailCall = async (accessToken: string, guardrailData: any) => {
'''
    networking_source = networking.read_text(encoding="utf-8")
    networking_start = networking_source.find(networking_anchor)
    if networking_start < 0:
        raise RuntimeError("TaskLattice networking: createGuardrailCall anchor missing")
    networking_end_marker = "\n};\n\nexport const uiSpendLogDetailsCall"
    networking_end = networking_source.find(networking_end_marker, networking_start)
    if networking_end < 0:
        raise RuntimeError("TaskLattice networking: end anchor missing")
    insert_at = networking_end + len("\n};")
    networking_helpers = '''

type TaskLatticeGuardMode = "pre_call" | "post_call";
type TaskLatticeGuardFallback = "fail_closed" | "fail_open";
type TaskLatticeGuardMessageChoice = "inherit" | "yes" | "no";

export type TaskLatticeGuardCreateInput = {
  guardrail_name: string;
  endpoint: string;
  secret: string;
  mode: TaskLatticeGuardMode[];
  default_on: boolean;
  skip_system_message_choice: TaskLatticeGuardMessageChoice;
  skip_tool_message_choice: TaskLatticeGuardMessageChoice;
  unreachable_fallback: TaskLatticeGuardFallback;
  timeout_seconds: number;
};

export type TaskLatticeGuardUpdateInput = Partial<TaskLatticeGuardCreateInput>;

export type TaskLatticeGuardView = {
  guardrail_id: string;
  guardrail_name: string;
  provider: "tasklattice_guard";
  endpoint: string;
  credential_configured: boolean;
  mode: TaskLatticeGuardMode[];
  default_on: boolean;
  skip_system_message_choice: TaskLatticeGuardMessageChoice;
  skip_tool_message_choice: TaskLatticeGuardMessageChoice;
  unreachable_fallback: TaskLatticeGuardFallback;
  timeout_seconds: number;
};

const taskLatticeGuardRequest = async <T,>(
  accessToken: string,
  method: "POST" | "PATCH",
  path: string,
  body: TaskLatticeGuardCreateInput | TaskLatticeGuardUpdateInput,
): Promise<T> => {
  const url = proxyBaseUrl ? `${proxyBaseUrl}${path}` : path;
  const response = await fetch(url, {
    method,
    headers: {
      [globalLitellmHeaderName]: `Bearer ${accessToken}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify(body),
  });
  if (!response.ok) {
    const errorText = await response.text();
    handleError(errorText);
    throw new Error(errorText);
  }
  return (await response.json()) as T;
};

export const createTaskLatticeGuardCall = (
  accessToken: string,
  input: TaskLatticeGuardCreateInput,
) => taskLatticeGuardRequest<TaskLatticeGuardView>(accessToken, "POST", "/guardrails/tasklattice", input);

export const updateTaskLatticeGuardCall = (
  accessToken: string,
  guardrailId: string,
  input: TaskLatticeGuardUpdateInput,
) => taskLatticeGuardRequest<TaskLatticeGuardView>(
  accessToken,
  "PATCH",
  `/guardrails/tasklattice/${encodeURIComponent(guardrailId)}`,
  input,
);
'''
    networking.write_text(
        networking_source[:insert_at]
        + networking_helpers
        + networking_source[insert_at:],
        encoding="utf-8",
    )

    replace_once(
        provider_fields,
        '''  value?: Record<string, any> | null;
}
''',
        '''  value?: Record<string, any> | null;
  optionalFields?: string[];
}
''',
        "native provider optional edit fields",
    )
    replace_once(
        provider_fields,
        '''  providerParams: providerParamsProp = null,
  value = null,
}) => {
''',
        '''  providerParams: providerParamsProp = null,
  value = null,
  optionalFields = [],
}) => {
''',
        "native provider optional edit field input",
    )
    replace_once(
        provider_fields,
        '''          rules={field.required ? [{ required: true, message: `${fieldKey} is required` }] : undefined}
''',
        '''          rules={field.required && !optionalFields.includes(fullFieldKey)
            ? [{ required: true, message: `${fieldKey} is required` }]
            : undefined}
''',
        "native provider optional edit validation",
    )

    replace_once(
        add_form,
        'import { createGuardrailCall, getGuardrailProviderSpecificParams, getGuardrailUISettings, modelAvailableCall } from "../networking";',
        'import { createGuardrailCall, createTaskLatticeGuardCall, getGuardrailProviderSpecificParams, getGuardrailUISettings, modelAvailableCall } from "../networking";',
        "TaskLattice create API import",
    )
    replace_once(
        add_form,
        '''interface GuardrailPreset {
  provider: string;
  categoryName?: string;
  guardrailNameSuggestion: string;
  mode: string;
  defaultOn: boolean;
}
''',
        '''interface GuardrailPreset {
  provider: string;
  categoryName?: string;
  guardrailNameSuggestion: string;
  mode: string | string[];
  defaultOn: boolean;
}
''',
        "TaskLattice preset modes",
    )
    replace_once(
        add_form,
        '''  const isToolPermissionProvider = useMemo(() => {
    if (!selectedProvider) {
      return false;
    }
    const providerValue = guardrail_provider_map[selectedProvider];
    return (providerValue || "").toLowerCase() === "tool_permission";
  }, [selectedProvider]);
''',
        '''  const isToolPermissionProvider = useMemo(() => {
    if (!selectedProvider) {
      return false;
    }
    const providerValue = guardrail_provider_map[selectedProvider];
    return (providerValue || "").toLowerCase() === "tool_permission";
  }, [selectedProvider]);

  const isTaskLatticeProvider = useMemo(
    () => Boolean(selectedProvider && guardrail_provider_map[selectedProvider] === "tasklattice_guard"),
    [selectedProvider],
  );
''',
        "TaskLattice provider state",
    )
    replace_once(
        add_form,
        '''    const baseValues: Record<string, any> = {
      provider: preset.provider,
      guardrail_name: preset.guardrailNameSuggestion,
      mode: preset.mode,
      default_on: preset.defaultOn,
      skip_system_message_choice: "inherit",
      skip_tool_message_choice: "inherit",
    };
''',
        '''    const baseValues: Record<string, any> = {
      provider: preset.provider,
      guardrail_name: preset.guardrailNameSuggestion,
      mode: preset.mode,
      default_on: preset.defaultOn,
      skip_system_message_choice: "inherit",
      skip_tool_message_choice: "inherit",
      ...(preset.provider === "TasklatticeGuard" && {
        optional_params: { unreachable_fallback: "fail_closed", timeout_seconds: 10 },
      }),
    };
''',
        "TaskLattice native preset defaults",
    )
    replace_once(
        add_form,
        '''    if (value === "BlockCodeExecution") {
      resetValues.confidence_threshold = 0.5;
    }
    form.setFieldsValue(resetValues);
''',
        '''    if (value === "BlockCodeExecution") {
      resetValues.confidence_threshold = 0.5;
    }
    if (guardrail_provider_map[value] === "tasklattice_guard") {
      Object.assign(resetValues, {
        guardrail_name: "TaskLattice Guard",
        mode: ["pre_call", "post_call"],
        default_on: true,
        optional_params: { unreachable_fallback: "fail_closed", timeout_seconds: 10 },
      });
    }
    form.setFieldsValue(resetValues);
''',
        "TaskLattice native form defaults",
    )
    replace_once(
        add_form,
        '''          const fieldsToValidate = ["guardrail_name", "provider", "mode", "default_on"];

          if (selectedProvider === "PresidioPII") {
''',
        '''          const fieldsToValidate = ["guardrail_name", "provider", "mode", "default_on"];

          if (isTaskLatticeProvider) {
            fieldsToValidate.push("api_base", "api_key");
          }
          if (selectedProvider === "PresidioPII") {
''',
        "TaskLattice required native provider fields",
    )
    replace_once(
        add_form,
        '''      // Prepare the guardrail data with proper typings
      const guardrailData: {
''',
        '''      if (guardrailProvider === "tasklattice_guard") {
        if (!accessToken) throw new Error("No access token available");
        const options = values.optional_params || {};
        await createTaskLatticeGuardCall(accessToken, {
          guardrail_name: values.guardrail_name,
          endpoint: values.api_base,
          secret: values.api_key,
          mode: values.mode,
          default_on: values.default_on,
          skip_system_message_choice: values.skip_system_message_choice,
          skip_tool_message_choice: values.skip_tool_message_choice,
          unreachable_fallback: options.unreachable_fallback ?? "fail_closed",
          timeout_seconds: options.timeout_seconds ?? 10,
        });
        NotificationsManager.success("Guardrail created successfully");
        resetForm();
        onSuccess();
        onClose();
        return;
      }

      // Prepare the guardrail data with proper typings
      const guardrailData: {
''',
        "TaskLattice dedicated create submission",
    )
    replace_once(
        add_form,
        '''            {guardrailSettings?.supported_modes?.map((mode) => (
''',
        '''            {guardrailSettings?.supported_modes
              ?.filter((mode) => !isTaskLatticeProvider || ["pre_call", "post_call"].includes(mode))
              .map((mode) => (
''',
        "TaskLattice supported mode filter",
    )

    replace_once(
        info_view,
        '''  updateGuardrailCall,
} from "@/components/networking";
''',
        '''  updateGuardrailCall,
  type TaskLatticeGuardUpdateInput,
  updateTaskLatticeGuardCall,
} from "@/components/networking";
''',
        "TaskLattice update API import",
    )
    replace_once(
        info_view,
        '''  getGuardrailLogoAndName,
  guardrail_provider_map,
''',
        '''  formatGuardrailModes,
  getGuardrailLogoAndName,
  guardrail_provider_map,
''',
        "shared mode formatter import",
    )
    replace_once(
        info_view,
        '''      // Prepare update data object - only include changed fields
      const updateData: any = {
''',
        '''      if (guardrailData.litellm_params?.guardrail === "tasklattice_guard") {
        const updateData: TaskLatticeGuardUpdateInput = {};
        const options = values.optional_params || {};
        const endpoint = values.api_base?.trim();
        const secret = values.api_key?.trim();
        if (values.guardrail_name !== guardrailData.guardrail_name) {
          updateData.guardrail_name = values.guardrail_name;
        }
        if (endpoint && endpoint !== guardrailData.litellm_params?.api_base) {
          updateData.endpoint = endpoint;
        }
        if (secret) updateData.secret = secret;
        if (JSON.stringify(values.mode) !== JSON.stringify(guardrailData.litellm_params?.mode)) {
          updateData.mode = values.mode;
        }
        if (values.default_on !== guardrailData.litellm_params?.default_on) {
          updateData.default_on = values.default_on;
        }
        const currentSystemChoice = skipSystemMessageToChoice(
          guardrailData.litellm_params?.skip_system_message_in_guardrail,
        );
        if (values.skip_system_message_choice !== currentSystemChoice) {
          updateData.skip_system_message_choice = values.skip_system_message_choice;
        }
        const currentToolChoice = skipToolMessageToChoice(
          guardrailData.litellm_params?.skip_tool_message_in_guardrail,
        );
        if (values.skip_tool_message_choice !== currentToolChoice) {
          updateData.skip_tool_message_choice = values.skip_tool_message_choice;
        }
        if (options.unreachable_fallback !== guardrailData.litellm_params?.unreachable_fallback) {
          updateData.unreachable_fallback = options.unreachable_fallback;
        }
        if (options.timeout_seconds !== guardrailData.litellm_params?.timeout_seconds) {
          updateData.timeout_seconds = options.timeout_seconds;
        }
        if (Object.keys(updateData).length === 0) {
          NotificationsManager.info("No changes detected");
          setIsEditing(false);
          return;
        }
        await updateTaskLatticeGuardCall(accessToken, guardrailId, updateData);
        NotificationsManager.success(
          updateData.endpoint || updateData.secret
            ? "TaskLattice Guard connection verified and updated"
            : "Guardrail updated successfully",
        );
        await fetchGuardrailInfo();
        form.setFieldValue("api_key", "");
        setIsEditing(false);
        return;
      }

      // Prepare update data object - only include changed fields
      const updateData: any = {
''',
        "TaskLattice dedicated update submission",
    )
    replace_once(
        info_view,
        '''                  <Title>{guardrailData.litellm_params?.mode || "-"}</Title>
''',
        '''                  <Title>{formatGuardrailModes(guardrailData.litellm_params?.mode)}</Title>
''',
        "Guardrail overview modes",
    )
    replace_once(
        info_view,
        '''                      <div>{guardrailData.litellm_params?.mode || "-"}</div>
''',
        '''                      <div>{formatGuardrailModes(guardrailData.litellm_params?.mode)}</div>
''',
        "Guardrail settings modes",
    )
    replace_once(
        info_view,
        '''                    <Form.Item label="Default On" name="default_on">
                      <Select>
                        <Select.Option value={true}>Yes</Select.Option>
                        <Select.Option value={false}>No</Select.Option>
                      </Select>
                    </Form.Item>

''',
        '''                    <Form.Item label="Default On" name="default_on">
                      <Select>
                        <Select.Option value={true}>Yes</Select.Option>
                        <Select.Option value={false}>No</Select.Option>
                      </Select>
                    </Form.Item>

                    {guardrailData.litellm_params?.guardrail === "tasklattice_guard" && (
                      <Form.Item
                        label="Mode"
                        name="mode"
                        tooltip="How the guardrail should be applied"
                        rules={[{ required: true, message: "Please select a mode" }]}
                      >
                        <Select mode="multiple">
                          {guardrailSettings?.supported_modes
                            ?.filter((mode) => ["pre_call", "post_call"].includes(mode))
                            .map((mode) => (
                              <Select.Option key={mode} value={mode}>{mode}</Select.Option>
                            ))}
                        </Select>
                      </Form.Item>
                    )}

''',
        "TaskLattice native mode editor",
    )
    replace_once(
        info_view,
        '''                          value={guardrailData.litellm_params}
                        />
''',
        '''                          value={guardrailData.litellm_params}
                          optionalFields={
                            guardrailData.litellm_params?.guardrail === "tasklattice_guard" ? ["api_key"] : []
                          }
                        />
''',
        "TaskLattice optional credential rotation",
    )
    replace_once(
        info_view,
        '''                    <Divider orientation="left">Advanced Settings</Divider>
                    <Form.Item label="Guardrail Information" name="guardrail_info">
                      <Input.TextArea rows={5} />
                    </Form.Item>
''',
        '''                    {guardrailData.litellm_params?.guardrail !== "tasklattice_guard" && (
                      <>
                        <Divider orientation="left">Advanced Settings</Divider>
                        <Form.Item label="Guardrail Information" name="guardrail_info">
                          <Input.TextArea rows={5} />
                        </Form.Item>
                      </>
                    )}
''',
        "hide managed TaskLattice metadata",
    )

    replace_once(
        guardrail_table,
        '''  getGuardrailLogoAndName,
  guardrail_provider_map,
''',
        '''  formatGuardrailModes,
  getGuardrailLogoAndName,
  guardrail_provider_map,
''',
        "Guardrail table mode formatter import",
    )
    replace_once(
        guardrail_table,
        '''        return <span className="text-xs">{guardrail.litellm_params.mode}</span>;
''',
        '''        return <span className="text-xs">{formatGuardrailModes(guardrail.litellm_params.mode)}</span>;
''',
        "Guardrail table modes",
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path("/app"))
    args = parser.parse_args()
    try:
        version = importlib.metadata.version("litellm")
    except importlib.metadata.PackageNotFoundError:
        # Host-only dry runs can opt into the reviewed version explicitly.
        version = os.environ.get("TASKLATTICE_LITELLM_SOURCE_VERSION", "missing")
    if version != EXPECTED_VERSION:
        raise RuntimeError(
            f"Refusing to patch LiteLLM {version}; reviewed version is {EXPECTED_VERSION}"
        )
    app_root = args.root.resolve()
    version_dir = Path(__file__).resolve().parent
    copy_overlay(version_dir, app_root)
    patch_backend(app_root)
    patch_ui(app_root)
    print(f"Applied TaskLattice Guard overlay to LiteLLM {version}")


if __name__ == "__main__":
    main()
