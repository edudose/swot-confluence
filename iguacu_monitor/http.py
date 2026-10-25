"""Cliente HTTP con reintentos, pensado para servicios públicos inestables.

Las APIs de ANA y los portales de INPE/ONS devuelven 5xx y cortan conexiones
con cierta frecuencia; reintentar con espera exponencial es lo que hace la
diferencia entre un monitor que corre solo y uno que hay que vigilar.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import requests

log = logging.getLogger(__name__)

USER_AGENT = "iguacu-monitor/0.1 (monitoreo hidrológico; contacto: operador)"

RETRY_STATUS = {429, 500, 502, 503, 504}


class FetchError(RuntimeError):
    """La fuente no pudo ser leída tras agotar los reintentos."""


def request(
    method: str,
    url: str,
    *,
    params: dict[str, Any] | None = None,
    data: dict[str, Any] | None = None,
    timeout: float = 60,
    attempts: int = 4,
    backoff: float = 2.0,
    stream: bool = False,
    headers: dict[str, str] | None = None,
    session: requests.Session | None = None,
) -> requests.Response:
    sess = session or requests
    hdrs = {"User-Agent": USER_AGENT}
    if headers:
        hdrs.update(headers)

    last: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            resp = sess.request(method, url, params=params, data=data, timeout=timeout,
                                stream=stream, headers=hdrs)
            if resp.status_code in RETRY_STATUS and attempt < attempts:
                wait = backoff ** attempt
                log.warning("%s devolvió %s; reintento %d/%d en %.0fs",
                            url, resp.status_code, attempt, attempts, wait)
                time.sleep(wait)
                continue
            return resp
        except requests.RequestException as exc:  # timeout, DNS, reset
            last = exc
            if attempt >= attempts:
                break
            wait = backoff ** attempt
            log.warning("%s falló (%s); reintento %d/%d en %.0fs",
                        url, exc.__class__.__name__, attempt, attempts, wait)
            time.sleep(wait)

    raise FetchError(f"no pude leer {url} tras {attempts} intentos: {last}")


def get(url: str, **kwargs: Any) -> requests.Response:
    return request("GET", url, **kwargs)


def post(url: str, **kwargs: Any) -> requests.Response:
    return request("POST", url, **kwargs)
