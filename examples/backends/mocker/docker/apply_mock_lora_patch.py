#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
#
# Idempotently patch the *installed* ``dynamo.mocker.main`` (e.g. PyPI ai-dynamo)
# to start optional mock LoRA admin HTTP when MOCKER_MOCK_LORA_ADMIN_PORT is set.
#
# Supports:
# - Legacy layout (ai-dynamo 1.0.x): ``DistributedRuntime`` + ``worker_engine_args_path``
# - Current layout: ``create_runtime`` + ``build_runtime_config(worker_engine_args)``
#
# Do not overlay the whole ``dynamo.mocker`` package from git onto an older wheel:
# ``dynamo.llm`` exports differ and break imports (e.g. MockEngineArgs).

from __future__ import annotations

import pathlib
import sys


def _mocker_main_path() -> pathlib.Path:
    import dynamo.mocker

    return pathlib.Path(dynamo.mocker.__file__).parent / "main.py"


MARKER = "        # DYNAMO_DOCKER_MOCK_LORA_BEGIN\n"

PATCH_LEGACY = (
    MARKER
    + """        try:
            from dynamo.llm import ModelRuntimeConfig
            from dynamo.mocker.mock_lora_http import (
                mock_lora_admin_host,
                mock_lora_admin_port_for_worker,
                start_mock_lora_admin_http,
            )
        except ImportError:
            pass
        else:
            _mock_lora_admin_port = mock_lora_admin_port_for_worker(worker_id)
            if _mock_lora_admin_port is not None:
                import json as _json_ml

                _wargs_ml: dict = {}
                try:
                    with open(worker_engine_args_path) as _f_ml:
                        _wargs_ml = _json_ml.load(_f_ml)
                except Exception:
                    pass
                _bs_ml = int(_wargs_ml.get("block_size") or 64) or 64
                _rc_ml = ModelRuntimeConfig()
                _ngb = _wargs_ml.get("num_gpu_blocks")
                if _ngb is not None:
                    _rc_ml.total_kv_blocks = int(_ngb)
                _mns = _wargs_ml.get("max_num_seqs")
                if _mns is not None:
                    _rc_ml.max_num_seqs = int(_mns)
                _mxt_ml = _wargs_ml.get("max_num_batched_tokens")
                if _mxt_ml is not None:
                    _rc_ml.max_num_batched_tokens = int(_mxt_ml)
                if "enable_local_indexer" in _wargs_ml:
                    _rc_ml.enable_local_indexer = bool(_wargs_ml["enable_local_indexer"])
                _dps_ml = _wargs_ml.get("dp_size")
                if _dps_ml is not None:
                    _rc_ml.data_parallel_size = int(_dps_ml)
                start_mock_lora_admin_http(
                    loop=loop,
                    runtime=runtime,
                    endpoint_dyn=args.endpoint,
                    base_model_path=args.model_path,
                    kv_cache_block_size=_bs_ml,
                    runtime_config=_rc_ml,
                    is_prefill=args.is_prefill_worker,
                    host=mock_lora_admin_host(),
                    port=_mock_lora_admin_port,
                )
        # DYNAMO_DOCKER_MOCK_LORA_END

"""
)

PATCH_MODERN = (
    MARKER
    + """        try:
            from dynamo.mocker.mock_lora_http import (
                mock_lora_admin_host,
                mock_lora_admin_port_for_worker,
                start_mock_lora_admin_http,
            )
        except ImportError:
            pass
        else:
            _mock_lora_admin_port = mock_lora_admin_port_for_worker(worker_id)
            if _mock_lora_admin_port is not None:
                start_mock_lora_admin_http(
                    loop=loop,
                    runtime=runtime,
                    endpoint_dyn=args.endpoint,
                    base_model_path=args.model_path,
                    kv_cache_block_size=kv_cache_block_size or 64,
                    runtime_config=runtime_config,
                    is_prefill=args.is_prefill_worker,
                    host=mock_lora_admin_host(),
                    port=_mock_lora_admin_port,
                )
        # DYNAMO_DOCKER_MOCK_LORA_END

"""
)


def main() -> int:
    path = _mocker_main_path()
    text = path.read_text(encoding="utf-8")
    if MARKER in text:
        print(f"[apply_mock_lora_patch] already patched: {path}")
        return 0
    if "start_mock_lora_admin_http" in text:
        print(
            f"[apply_mock_lora_patch] main.py already integrates mock LoRA; skip: {path}"
        )
        return 0

    needle_modern = (
        "        kv_cache_block_size, runtime_config = build_runtime_config(worker_engine_args)\n"
        "\n"
        "        # Create EntrypointArgs for this worker\n"
    )
    needle_legacy = (
        "        else:\n"
        "            worker_engine_args_path = extra_engine_args_path\n"
        "\n"
        "        # Create EntrypointArgs for this worker\n"
    )

    if needle_modern in text:
        repl_modern = (
            "        kv_cache_block_size, runtime_config = build_runtime_config(worker_engine_args)\n"
            + PATCH_MODERN
            + "        # Create EntrypointArgs for this worker\n"
        )
        text = text.replace(needle_modern, repl_modern, 1)
        path.write_text(text, encoding="utf-8")
        print(f"[apply_mock_lora_patch] applied modern-layout patch to {path}")
        return 0

    if needle_legacy in text:
        repl_legacy = (
            "        else:\n"
            "            worker_engine_args_path = extra_engine_args_path\n"
            + PATCH_LEGACY
            + "        # Create EntrypointArgs for this worker\n"
        )
        text = text.replace(needle_legacy, repl_legacy, 1)
        path.write_text(text, encoding="utf-8")
        print(f"[apply_mock_lora_patch] applied legacy-layout patch to {path}")
        return 0

    print(
        f"[apply_mock_lora_patch] WARN: unrecognized mocker main.py; skip patch: {path}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
