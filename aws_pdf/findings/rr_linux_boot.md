# RocketRide engine 3.3.0 / 3.3.1 cannot boot on Linux

**Severity:** blocking — the engine never starts. Affects every non-macOS host.
**Found:** 2026-08-14, native x86_64 (c7i.8xlarge, Ubuntu 22.04, Docker 29.7.2),
engine `3.3.1-linux-x64`, in a clean container build.
**Not an emulation artifact:** this is a native x86_64 Linux host, no Rosetta,
no QEMU.

## Symptom

The engine bootstraps pip/wheel/setuptools/uv, then dies compiling
dependency constraints before serving anything:

```
[65MB] Compiling constraints...
[65MB] Error: Error (ap:0x12) /opt/rocketride/engine/lib/python3.12/depends.py:702
       Message: Failed to compile constraints:
       × No solution found when resolving dependencies:
       ╰─▶ Because there is no version of onnxruntime-gpu{sys_platform != 'darwin'}==1.20.1
           and you require onnxruntime-gpu{sys_platform != 'darwin'}==1.20.1,
           we can conclude that your requirements are unsatisfiable.
RuntimeError: Failed to compile constraints
[65MB] Exit: Python (ap:0x3c) init.cpp:559
```

The container then exits. No pipeline is ever served.

## Root cause

The pin appears in **five** requirements files, spanning `ai/` and `nodes/`:

```
ai/common/models/audio/requirements_whisper.txt:9
ai/common/models/vision/requirements_pose.txt:8
ai/common/models/gliner/requirements_gliner.txt:7
nodes/audio_transcribe/requirements.txt:10
nodes/anonymize/requirements.txt:4
```

each carrying:

```
onnxruntime-gpu==1.20.1; platform_system != 'Darwin'
onnxruntime==1.20.1;     platform_system == 'Darwin'
```

**`onnxruntime-gpu` 1.20.1 was never published to PyPI.** The published
versions around it are `1.20.0` and `1.20.2`. The CPU package `onnxruntime`
*does* have a 1.20.1. Verified against the PyPI JSON API on 2026-08-14:

| package | 1.20.1 on PyPI |
|---|---|
| `onnxruntime` (Darwin branch) | **yes** |
| `onnxruntime-gpu` (non-Darwin branch) | **no** — only 1.20.0, 1.20.2 |

So the two branches of the same version pin do not have matching availability:
the macOS path resolves, the Linux path cannot.

## Why it is fatal rather than degraded

Two things combine:

1. **The pin is in an opt-in feature.** That same file is annotated
   `contract-check: skip-install  reason: ~300 MB of NER deps ... for an
   opt-in feature. PR lane skips`. The benchmark pipeline
   (`webhook → parse → preprocessor_langchain → embedding_transformer →
   response_documents`) never loads GLiNER.
2. **Constraint compilation is global and happens at boot.** `ai/__init__.py`
   calls `depends(CONST_AI_REQUIREMENTS)` at import, which compiles *all*
   requirements files together. One unsatisfiable pin anywhere therefore
   prevents the engine from starting at all, even for pipelines that use none
   of it.

An unsatisfiable pin in an explicitly optional, explicitly skip-install
feature takes down the whole engine.

## Why it was not caught earlier

The marker split hides it on the developer platform. On macOS the Darwin
branch (`onnxruntime==1.20.1`) resolves fine, so a native macOS engine —
which is what this project had been validating against — boots normally and
shows no sign of the problem. It only appears on Linux, which is the only
platform the engine actually ships a server build for.

## Affected versions

Confirmed by inspecting the released linux-x64 tarballs:

- `server-v3.3.1` — affected (boot failure reproduced)
- `server-v3.3.0` — affected (same pin present in the tarball)
- `server-v3.2.1` — not affected in practice; this project's earlier Docker
  runs booted on it

## Suggested fix

Bump the GPU pin to a published version:

```diff
-onnxruntime-gpu==1.20.1; platform_system != 'Darwin'
+onnxruntime-gpu==1.20.2; platform_system != 'Darwin'
```

Worth considering separately: whether boot-time constraint compilation should
be fatal for requirements belonging to opt-in features that are not being
loaded. Today a single bad pin in any optional feature is a total outage.

## Workaround used in this benchmark

`rocketride/Dockerfile` rewrites that pin to `1.20.2` at image build, after
extracting the engine and before first boot:

```dockerfile
RUN find "$ENGINE_DIR/ai" -name 'requirements*.txt' -exec \
      sed -i 's/onnxruntime-gpu==1\.20\.1/onnxruntime-gpu==1.20.2/g' {} +
```

Nothing else is modified. This is recorded in run provenance so results are
never mistaken for stock-3.3.1 behaviour, and should be removed once upstream
ships a fix.
