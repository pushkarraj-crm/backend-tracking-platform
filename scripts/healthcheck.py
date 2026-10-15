import os
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

request = Request(
    "http://127.0.0.1:8000/health/ready",
    headers={"Host": urlsplit(os.environ["PUBLIC_BASE_URL"]).netloc},
)
with urlopen(request, timeout=8) as response:
    if response.status != 200:
        raise SystemExit(1)
