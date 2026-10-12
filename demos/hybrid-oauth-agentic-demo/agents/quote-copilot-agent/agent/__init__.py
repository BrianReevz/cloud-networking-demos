# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0

import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

try:
    import urllib3.contrib.pyopenssl

    urllib3.contrib.pyopenssl.extract_from_urllib3()
except Exception:
    pass

import google.auth

from . import agent  # noqa: F401

try:
    _, project_id = google.auth.default()
    if project_id:
        os.environ.setdefault("GOOGLE_CLOUD_PROJECT", project_id)
except Exception:
    pass

os.environ.setdefault("GOOGLE_CLOUD_LOCATION", "global")
os.environ.setdefault("GOOGLE_GENAI_USE_VERTEXAI", "True")
