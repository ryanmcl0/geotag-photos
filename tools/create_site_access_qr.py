#!/usr/bin/env python3
"""Create or rotate the private QR invitation for the site password gate.

    ./tools/create_site_access_qr.py              new token: previous QR codes stop
                                                  working once the secret is published
    ./tools/create_site_access_qr.py --keep-token re-draw the QR for the current token
                                                  (e.g. after a domain change); old
                                                  codes keep working
"""

from __future__ import annotations

import os
import re
import secrets
import sys
import tempfile
from pathlib import Path

import cv2


ENV_PATH = Path('.env.deploy')
OUTPUT_PATH = Path('site-access-qr.png')
SECRET_NAME = 'CF_QR_ACCESS_TOKEN'


def env_value(text: str, name: str) -> str | None:
    match = re.search(
        rf'(?m)^\s*(?:export\s+)?{re.escape(name)}\s*=\s*([^\n]*)$', text
    )
    if not match:
        return None
    value = match.group(1).strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1]
    return value


def atomic_write(path: Path, data: bytes, mode: int | None = None) -> None:
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as temporary:
        temporary.write(data)
        temporary_path = Path(temporary.name)
    if mode is not None:
        temporary_path.chmod(mode)
    os.replace(temporary_path, path)


def main() -> None:
    if not ENV_PATH.exists():
        raise SystemExit('Missing .env.deploy')

    environment = ENV_PATH.read_text()
    project = env_value(environment, 'CF_PAGES_PROJECT')
    site_url = env_value(environment, 'CF_SITE_URL')
    if not (site_url or project):
        raise SystemExit('CF_SITE_URL / CF_PAGES_PROJECT is missing from .env.deploy')
    site_url = (site_url or f'https://{project}.pages.dev').rstrip('/')

    keep = '--keep-token' in sys.argv[1:]
    token = env_value(environment, SECRET_NAME) if keep else secrets.token_urlsafe(32)
    if not token:
        raise SystemExit(f'--keep-token: no {SECRET_NAME} in .env.deploy yet')
    invitation_url = f'{site_url}/login#qr={token}'

    parameters = cv2.QRCodeEncoder_Params()
    parameters.correction_level = cv2.QRCodeEncoder_CORRECT_LEVEL_H
    code = cv2.QRCodeEncoder_create(parameters).encode(invitation_url)
    code = cv2.copyMakeBorder(code, 4, 4, 4, 4, cv2.BORDER_CONSTANT, value=255)
    code = cv2.resize(code, None, fx=12, fy=12, interpolation=cv2.INTER_NEAREST)
    ok, png = cv2.imencode('.png', code)
    if not ok:
        raise SystemExit('Could not encode QR code as PNG')

    secret_line = f'export {SECRET_NAME}="{token}"'
    pattern = rf'(?m)^(?:export\s+)?{re.escape(SECRET_NAME)}=.*$'
    if re.search(pattern, environment):
        environment = re.sub(pattern, secret_line, environment)
    else:
        environment = environment.rstrip('\n') + '\n' + secret_line + '\n'

    atomic_write(OUTPUT_PATH, png.tobytes())
    print(f'Created {OUTPUT_PATH.resolve()} -> {site_url}/login')
    if keep:
        print(f'Kept the existing {SECRET_NAME}: earlier QR codes still work.')
        return
    atomic_write(ENV_PATH, environment.encode(), ENV_PATH.stat().st_mode)
    print(f'Updated {SECRET_NAME} in .env.deploy (value hidden)')
    print('Run this tool again to create a replacement, then publish the updated')
    print(f'{SECRET_NAME} secret to revoke the previous QR code.')


if __name__ == '__main__':
    main()
