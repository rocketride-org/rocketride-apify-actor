"""Build-time preparation of the RocketRide engine image for Apify. Run with the engine's Python.

1. CPU-only PyTorch. The engine pins the CUDA build of torch, which drags in ~6 GB of
   GPU libraries. Apify runs Actors on CPU, so that only slows down the image pull.
   We switch the pin to the CPU build of the same version and keep every other
   dependency exactly as the engine release resolved it.
2. Installs dependencies that the ready-made pipelines need but engine 3.6.0 does not
   install on its own (see requirements-extra.txt).

It fails loudly if the engine layout changes, so we never silently ship the GPU build.
"""

import os
import re
import sys

import depends

HOME = os.environ.get('ROCKETRIDE_HOME', '/opt/rocketride')
TORCH_REQ = os.path.join(HOME, 'ai', 'common', 'torch', 'requirements.txt')
CACHE = depends.engine_cache_dir()
EXTRA = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'requirements-extra.txt')


def to_cpu(path: str, required: bool) -> bool:
    with open(path, encoding='utf-8') as f:
        old = f.read()
    new = re.sub(r'whl/cu\d+', 'whl/cpu', old)
    new = re.sub(r'\+cu\d+', '+cpu', new)
    if new == old:
        if required:
            sys.exit(f'FATAL: no CUDA torch pin found in {path}. The engine layout changed; update scripts/prepare_image.py.')
        return False
    with open(path, 'w', encoding='utf-8') as f:
        f.write(new)
    print(f'CPU-only torch: patched {path}')
    return True


def main() -> None:
    to_cpu(TORCH_REQ, required=True)
    constraints = os.path.join(CACHE, 'constraints.txt')
    if os.path.exists(constraints):
        to_cpu(constraints, required=True)
        to_cpu(os.path.join(CACHE, 'combined.txt'), required=False)
        # The engine recompiles all constraints when requirement files change (it hashes
        # their size and mtime). Record the new state so the released versions are kept.
        files = depends._find_requirement_files() + depends._find_override_files()
        depends._save_hash(os.path.join(CACHE, 'requirements.hash'), depends._compute_hash(files))
        before = os.stat(constraints).st_mtime_ns
        depends.ensure_constraints()
        if os.stat(constraints).st_mtime_ns != before:
            print('WARNING: the engine recompiled its constraints; dependency versions may differ from the release.')
        with open(constraints, encoding='utf-8') as f:
            text = f.read()
        if 'whl/cpu' not in text or re.search(r'\+cu\d+', text):
            sys.exit('FATAL: constraints still reference the CUDA torch build.')
    else:
        print('No compiled constraints found; the engine will compile them with the CPU pin.')

    depends.depends(EXTRA)
    print('prepare_image: done')


if __name__ == '__main__':
    main()
