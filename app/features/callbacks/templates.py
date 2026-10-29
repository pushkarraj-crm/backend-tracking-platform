from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from app.core.services import decrypt, encrypt
from app.features.campaigns.policy import TOKEN


def stored_url(endpoint):
    return decrypt(endpoint.url[7:]) if endpoint.url.startswith("enc:v1:") else endpoint.url


def store_url(value):
    return "enc:v1:" + encrypt(value)


def render_url(url, event):
    parts = urlsplit(url)
    values = {
        **event.get("tracking", {}),
        **event.get("advertiser_parameters", {}),
        **{k: v for k, v in event.items() if k not in {"tracking", "advertiser_parameters"}},
        "tid": event.get("click_id", ""),
        "event_token": event.get("event_key", ""),
    }
    query = [
        (k, TOKEN.sub(lambda m: str(values.get(m[1], "") or ""), v))
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
    ]
    return urlunsplit(parts._replace(query=urlencode(query)))
