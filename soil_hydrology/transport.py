"""Accès HTTP borné et cache de métadonnées, séparé du parsing."""
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


def session():
    result = requests.Session()
    retry = Retry(total=2, backoff_factor=0.5, status_forcelist=[429, 500, 502, 503, 504],
                  allowed_methods=["GET", "HEAD"], respect_retry_after_header=False)
    result.mount("https://", HTTPAdapter(max_retries=retry))
    return result


def cached_document(http, cache, url, offline=False, refresh=False, ttl=86400):
    key = cache.key("document-v1", url)
    data = cache.get(key, None if offline else (0 if refresh else ttl))
    if data is not None:
        return data, offline
    if offline:
        raise RuntimeError("Métadonnées absentes du cache hors ligne")
    try:
        with http.get(url, timeout=(15, 60)) as response:
            response.raise_for_status()
            data = response.text
        cache.put(key, data)
        return data, False
    except requests.RequestException:
        data = cache.get(key)
        if data is None:
            raise
        return data, True
