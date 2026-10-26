#!/usr/bin/env python3
"""Apply the reviewed TaskLattice overlay to LiteLLM 1.106.4.

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

EXPECTED_VERSION = "1.106.4"


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
    replace_once(registry, '''guardrail_initializer_registry.update(_discovered_initializers)
''', """guardrail_initializer_registry.update(_discovered_initializers)

from litellm.proxy.guardrails.guardrail_hooks.tasklattice_guard import (
    guardrail_class_registry as tasklattice_guard_classes,
    guardrail_initializer_registry as tasklattice_guard_initializers,
)
guardrail_class_registry.update(tasklattice_guard_classes)
guardrail_initializer_registry.update(tasklattice_guard_initializers)
""", "explicit TaskLattice provider registration")
    replace_in_section(endpoints, '''async def create_guardrail(
''', '''

class UpdateGuardrailRequest''', '''    _reject_encrypted_litellm_params(request.guardrail.get("litellm_params"))
''', '''    _reject_encrypted_litellm_params(request.guardrail.get("litellm_params"))
    if (request.guardrail.get("litellm_params") or {}).get("guardrail") == "tasklattice_guard":
        raise HTTPException(status_code=400, detail="Use POST /guardrails/tasklattice for TaskLattice Guard")
''', "block generic TaskLattice creation")
    missing = '''        if existing_guardrail is None:
            raise HTTPException(status_code=404, detail=f"Guardrail with ID {guardrail_id} not found")
'''
    managed = '''        existing_params = existing_guardrail.get("litellm_params") or {}
        is_tasklattice = getattr(existing_params, "guardrail", None) == "tasklattice_guard" or (
            isinstance(existing_params, dict) and existing_params.get("guardrail") == "tasklattice_guard"
        )
'''
    for start, end in [
        ('''async def update_guardrail(
''', '''

@router.delete(
'''),
        ('''async def patch_guardrail(
''', '''

@router.get(
    "/guardrails/{guardrail_id}",'''),
    ]:
        replace_in_section(endpoints, start, end, missing, missing + managed + '''        if is_tasklattice:
            raise HTTPException(status_code=400, detail="Use PATCH /guardrails/tasklattice/{id} for TaskLattice Guard")
''', "block generic TaskLattice update")
    replace_in_section(endpoints, '''async def delete_guardrail(
''', '''

# --- Team guardrail registration''', missing, missing + managed + '''        if is_tasklattice:
            from litellm.proxy.guardrails.guardrail_hooks.tasklattice_guard.service import delete_tasklattice_guard_connection
            await delete_tasklattice_guard_connection(prisma_client=prisma_client, guardrail_id=guardrail_id)
            return {"message": f"Guardrail {guardrail_id} deleted successfully"}
''', "TaskLattice credential cleanup on standard delete")
    anchor = '''# Usage (dashboard) endpoints: overview, detail, logs
router.include_router(guardrails_usage_router)
'''
    replace_once(endpoints, anchor, '''from litellm.proxy.guardrails.guardrail_hooks.tasklattice_guard.endpoints import router as tasklattice_guard_router
router.include_router(tasklattice_guard_router)

''' + anchor, "register dedicated TaskLattice endpoints")


def patch_ui(app_root: Path) -> None:
    replace_once(
        app_root / 'ui/litellm-dashboard/src/app/(dashboard)/guardrails/_components/guardrail_info_helpers.tsx',
        'import xecguardLogo from "../../../../../public/assets/logos/xecguard.svg";',
        '''import xecguardLogo from "../../../../../public/assets/logos/xecguard.svg";
import taskLatticeGuardLogo from "../../../../../public/assets/logos/tasklattice_guard.svg";''',
        'TaskLattice logo import',
    )
    replace_once(
        app_root / 'ui/litellm-dashboard/src/app/(dashboard)/guardrails/_components/guardrail_info_helpers.tsx',
        '''export const guardrailLogoMap = {
''',
        '''export const guardrailLogoMap = {
  "TaskLattice Guard": taskLatticeGuardLogo.src,
''',
        'TaskLattice provider logo',
    )
    replace_once(
        app_root / 'ui/litellm-dashboard/src/app/(dashboard)/guardrails/_components/guardrail_info_helpers.tsx',
        '  return perProvider ?? settings?.supported_modes;',
        '''  if (providerKey === "tasklattice_guard") return ["pre_call", "post_call"];
  return perProvider ?? settings?.supported_modes;''',
        'TaskLattice supported modes',
    )
    replace_once(
        app_root / 'ui/litellm-dashboard/src/app/(dashboard)/guardrails/_components/guardrail_garden_data.ts',
        '''export const PARTNER_GUARDRAIL_CARDS: GuardrailCardInfo[] = [
''',
        '''export const PARTNER_GUARDRAIL_CARDS: GuardrailCardInfo[] = [
  {
    id: "tasklattice_guard",
    name: "TaskLattice Guard",
    description: "Apply governed, versioned AI safety policies to model input and output.",
    category: "partner",
    logo: guardrailLogoMap["TaskLattice Guard"],
    tags: ["Input & Output", "Configurable Fallback", "Multi-Gateway"],
    providerKey: "TasklatticeGuard",
  },
''',
        'TaskLattice Guardrail Garden card',
    )
    replace_once(
        app_root / 'ui/litellm-dashboard/src/app/(dashboard)/guardrails/_components/guardrail_garden_configs.ts',
        '''  // ── Partner Guardrails ──
  presidio: {''',
        '''  // ── Partner Guardrails ──
  tasklattice_guard: {
    provider: "TasklatticeGuard",
    guardrailNameSuggestion: "TaskLattice Guard",
    mode: ["pre_call", "post_call"],
    defaultOn: true,
  },
  presidio: {''',
        'TaskLattice native preset',
    )
    replace_once(
        app_root / 'ui/litellm-dashboard/src/components/networking.tsx',
        'export const createGuardrailCall = async (accessToken: string, guardrailData: any) => {',
        '''

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

export const createGuardrailCall = async (accessToken: string, guardrailData: any) => {''',
        'TaskLattice lifecycle calls',
    )
    replace_once(
        app_root / 'ui/litellm-dashboard/src/app/(dashboard)/guardrails/_components/guardrail_provider_fields.tsx',
        '''  value?: Record<string, unknown> | null;
''',
        '''  value?: Record<string, unknown> | null;
  optionalFields?: string[];
''',
        'optional credential edit type',
    )
    replace_once(
        app_root / 'ui/litellm-dashboard/src/app/(dashboard)/guardrails/_components/guardrail_provider_fields.tsx',
        '''  value = null,
''',
        '''  value = null,
  optionalFields = [],
''',
        'optional credential edit input',
    )
    replace_once(
        app_root / 'ui/litellm-dashboard/src/app/(dashboard)/guardrails/_components/guardrail_provider_fields.tsx',
        '          rules={fieldRules(field, fieldKey)}',
        '          rules={fieldRules({ ...field, required: field.required && !optionalFields.includes(fullFieldKey) }, fieldKey)}',
        'optional credential edit validation',
    )
    replace_once(
        app_root / 'ui/litellm-dashboard/src/app/(dashboard)/guardrails/_components/add_guardrail_form.tsx',
        '''  createGuardrailCall,
''',
        '''  createGuardrailCall,
  createTaskLatticeGuardCall,
''',
        'TaskLattice create API import',
    )
    replace_once(
        app_root / 'ui/litellm-dashboard/src/app/(dashboard)/guardrails/_components/add_guardrail_form.tsx',
        '    applyValues(form, baseValues);',
        '''    if (preset.provider === "TasklatticeGuard") {
      baseValues.optional_params = { unreachable_fallback: "fail_closed", timeout_seconds: 10 };
    }
    applyValues(form, baseValues);''',
        'TaskLattice preset defaults',
    )
    replace_once(
        app_root / 'ui/litellm-dashboard/src/app/(dashboard)/guardrails/_components/add_guardrail_form.tsx',
        '    applyValues(form, resetValues);',
        '''    if (newProviderKey === "tasklattice_guard") {
      Object.assign(resetValues, {
        guardrail_name: "TaskLattice Guard", mode: ["pre_call", "post_call"], default_on: true,
        optional_params: { unreachable_fallback: "fail_closed", timeout_seconds: 10 },
      });
    }
    applyValues(form, resetValues);''',
        'TaskLattice selected provider defaults',
    )
    replace_once(
        app_root / 'ui/litellm-dashboard/src/app/(dashboard)/guardrails/_components/add_guardrail_form.tsx',
        '''      // Prepare the guardrail data with proper typings
''',
        '''      if (guardrailProvider === "tasklattice_guard") {
        if (!accessToken) throw new Error("No access token available");
        const options = (values.optional_params ?? {}) as Record<string, unknown>;
        await createTaskLatticeGuardCall(accessToken, {
          guardrail_name: asText(values.guardrail_name),
          endpoint: asText(values.api_base), secret: asText(values.api_key),
          mode: toModeArray(values.mode) as ("pre_call" | "post_call")[],
          default_on: Boolean(values.default_on),
          skip_system_message_choice: asSkipChoice(values.skip_system_message_choice) ?? "inherit",
          skip_tool_message_choice: asSkipChoice(values.skip_tool_message_choice) ?? "inherit",
          unreachable_fallback: options.unreachable_fallback === "fail_open" ? "fail_open" : "fail_closed",
          timeout_seconds: Number(options.timeout_seconds ?? 10),
        });
        toast.success("Guardrail created successfully");
        resetForm(); onSuccess(); onClose();
        return;
      }

      // Prepare the guardrail data with proper typings
''',
        'TaskLattice dedicated create submission',
    )
    replace_once(
        app_root / 'ui/litellm-dashboard/src/app/(dashboard)/guardrails/_components/guardrail_info.tsx',
        '''  updateGuardrailCall,
''',
        '''  updateGuardrailCall,
  updateTaskLatticeGuardCall,
''',
        'TaskLattice update API import',
    )
    replace_once(
        app_root / 'ui/litellm-dashboard/src/app/(dashboard)/guardrails/_components/guardrail_info.tsx',
        'import { Logo } from "@/components/molecules/logo/Logo";',
        '''import { Logo } from "@/components/molecules/logo/Logo";
import { MultiSelect } from "@/components/shared/MultiSelect";''',
        'native mode control import',
    )
    replace_once(
        app_root / 'ui/litellm-dashboard/src/app/(dashboard)/guardrails/_components/guardrail_info.tsx',
        '    form.setValue("default_on", guardrailData.litellm_params?.default_on);',
        '''    form.setValue("default_on", guardrailData.litellm_params?.default_on);
    form.setValue("mode", toModeArray(guardrailData.litellm_params?.mode));''',
        'TaskLattice mode edit initial value',
    )
    replace_once(
        app_root / 'ui/litellm-dashboard/src/app/(dashboard)/guardrails/_components/guardrail_info.tsx',
        '      const { logging_only_scope_choice: scopeChoice, logging_only_continue_on_input_failure: continueFlag } = values;',
        '''      if (guardrailData.litellm_params?.guardrail === "tasklattice_guard") {
        const options = (values.optional_params ?? {}) as Record<string, unknown>;
        const endpoint = asText(values.api_base).trim();
        const secret = asText(values.api_key).trim();
        await updateTaskLatticeGuardCall(accessToken, guardrailId, {
          guardrail_name: asText(values.guardrail_name),
          ...(endpoint && endpoint !== guardrailData.litellm_params.api_base ? { endpoint } : {}),
          ...(secret ? { secret } : {}),
          mode: toModeArray(values.mode) as ("pre_call" | "post_call")[],
          default_on: Boolean(values.default_on),
          skip_system_message_choice: (values.skip_system_message_choice ?? "inherit") as SkipSystemMessageChoice,
          skip_tool_message_choice: (values.skip_tool_message_choice ?? "inherit") as SkipToolMessageChoice,
          unreachable_fallback: options.unreachable_fallback === "fail_open" ? "fail_open" : "fail_closed",
          timeout_seconds: Number(options.timeout_seconds ?? 10),
        });
        toast.success("Guardrail updated successfully");
        await fetchGuardrailInfo(); form.setValue("api_key", ""); setIsEditing(false);
        return;
      }
      const { logging_only_scope_choice: scopeChoice, logging_only_continue_on_input_failure: continueFlag } = values;''',
        'TaskLattice dedicated update submission',
    )
    replace_once(
        app_root / 'ui/litellm-dashboard/src/app/(dashboard)/guardrails/_components/guardrail_info.tsx',
        '''                        <StreamScopeFormField
''',
        '''                        {guardrailData.litellm_params?.guardrail === "tasklattice_guard" && (
                          <GuardrailField control={form.control} name="mode" label="Mode" rules={requiredRule("Please select a mode")}>
                            {({ id, value, onChange }) => (
                              <MultiSelect id={id} options={[{label: "pre_call", value: "pre_call"}, {label: "post_call", value: "post_call"}]}
                                value={toModeArray(value)} onValueChange={onChange} />
                            )}
                          </GuardrailField>
                        )}
                        <StreamScopeFormField
''',
        'TaskLattice native mode editor',
    )
    replace_once(
        app_root / 'ui/litellm-dashboard/src/app/(dashboard)/guardrails/_components/guardrail_info.tsx',
        '''                              value={guardrailData.litellm_params}
''',
        '''                              value={guardrailData.litellm_params}
                              optionalFields={guardrailData.litellm_params?.guardrail === "tasklattice_guard" ? ["api_key"] : []}
''',
        'TaskLattice optional credential rotation',
    )
    replace_once(
        app_root / 'ui/litellm-dashboard/src/app/(dashboard)/guardrails/_components/guardrail_info.tsx',
        '''                        <SectionHeading>Advanced Settings</SectionHeading>
                        <GuardrailField control={form.control} name="guardrail_info" label="Guardrail Information">
                          {({ ref, value, ...field }) => (
                            <Textarea {...field} ref={ref} value={asText(value)} rows={5} />
                          )}
                        </GuardrailField>''',
        '''                        {guardrailData.litellm_params?.guardrail !== "tasklattice_guard" && (<>
                        <SectionHeading>Advanced Settings</SectionHeading>
                        <GuardrailField control={form.control} name="guardrail_info" label="Guardrail Information">
                          {({ ref, value, ...field }) => (
                            <Textarea {...field} ref={ref} value={asText(value)} rows={5} />
                          )}
                        </GuardrailField>
                        </>)}''',
        'hide managed TaskLattice metadata',
    )
    replace_once(
        app_root / 'ui/litellm-dashboard/src/app/(dashboard)/guardrails/_components/add_guardrail_form.tsx',
        '<StreamScopeFormField control={form.control} modes={toModeArray(form.watch("mode"))} />',
        '{guardrail_provider_map[selectedProvider ?? ""] !== "tasklattice_guard" && (<StreamScopeFormField control={form.control} modes={toModeArray(form.watch("mode"))} />)}',
        'hide unsupported create StreamScopeFormField',
    )
    replace_once(
        app_root / 'ui/litellm-dashboard/src/app/(dashboard)/guardrails/_components/add_guardrail_form.tsx',
        '''<LoggingOnlyScopeField
          control={form.control}
          mode={watchedMode}
          directionalScopeSupported={directionalScopeSupported}
        />''',
        '''{guardrail_provider_map[selectedProvider ?? ""] !== "tasklattice_guard" && (<LoggingOnlyScopeField
          control={form.control}
          mode={watchedMode}
          directionalScopeSupported={directionalScopeSupported}
        />)}''',
        'hide unsupported create LoggingOnlyScopeField',
    )
    replace_once(
        app_root / 'ui/litellm-dashboard/src/app/(dashboard)/guardrails/_components/guardrail_info.tsx',
        '''<StreamScopeFormField
                          control={form.control}
                          modes={toModeArray(guardrailData.litellm_params?.mode)}
                        />''',
        '''{guardrailData.litellm_params?.guardrail !== "tasklattice_guard" && (<StreamScopeFormField
                          control={form.control}
                          modes={toModeArray(guardrailData.litellm_params?.mode)}
                        />)}''',
        'hide unsupported edit StreamScopeFormField',
    )
    replace_once(
        app_root / 'ui/litellm-dashboard/src/app/(dashboard)/guardrails/_components/guardrail_info.tsx',
        '''<LoggingOnlyScopeField
                          control={form.control}
                          mode={guardrailData.litellm_params?.mode}
                          directionalScopeSupported={directionalScopeSupported}
                        />''',
        '''{guardrailData.litellm_params?.guardrail !== "tasklattice_guard" && (<LoggingOnlyScopeField
                          control={form.control}
                          mode={guardrailData.litellm_params?.mode}
                          directionalScopeSupported={directionalScopeSupported}
                        />)}''',
        'hide unsupported edit LoggingOnlyScopeField',
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
