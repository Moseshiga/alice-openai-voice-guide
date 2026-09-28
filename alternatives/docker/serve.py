"""Запуск того же обработчика вебхука на своём сервере."""

from http.server import ThreadingHTTPServer

from api.index import handler


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 8000), handler).serve_forever()

