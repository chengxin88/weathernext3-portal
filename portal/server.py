"""Loopback HTTP service for the RMAPS-style WeatherNext 3 product page."""
import argparse
import json
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlsplit, parse_qs

import render
import downloads
import wind_projects
import ec_wind_projects

def project_module(query):
    model=query.get("model", "WN3")
    if model=="WN3":return wind_projects
    if model=="EC":return ec_wind_projects
    raise ValueError("模式须为WN3或EC")

STATIC = Path(__file__).resolve().parent / "static"


class Handler(BaseHTTPRequestHandler):
    server_version = "wn3-portal"

    def log_message(self, *args):
        pass

    def reply(self, status, content, kind, cache="no-store"):
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", cache)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(content)

    def error(self, status, message):
        self.reply(status, json.dumps({"error": message}, ensure_ascii=False).encode(),
                   "application/json; charset=utf-8")

    def do_HEAD(self):
        self.handle_request(head=True)

    def do_GET(self):
        self.handle_request()

    def handle_request(self, head=False):
        parts = urlsplit(self.path)
        path = parts.path
        query = {k: v[0] for k, v in parse_qs(parts.query).items()}
        try:
            if path in ("/", "/index.html"):
                self.reply(200, (STATIC / "index.html").read_bytes(), "text/html; charset=utf-8")
            elif path in ("/download", "/download/", "/download/index.html"):
                if path == "/download":
                    self.send_response(302)
                    self.send_header("Location", "download/")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                else:
                    self.reply(200, (STATIC / "download.html").read_bytes(), "text/html; charset=utf-8")
            elif path == "/api/download-catalog":
                self.reply(200, json.dumps(downloads.field_catalog(), ensure_ascii=False).encode(),
                           "application/json; charset=utf-8")
            elif path in ("/download/xinjiang-wind/", "/download/xinjiang-wind/index.html"):
                self.reply(200, (STATIC / "xinjiang-wind.html").read_bytes(), "text/html; charset=utf-8")
            elif path == "/api/wind-project":
                self.reply(200, json.dumps(project_module(query).catalog(query.get('interval', 'hourly')), ensure_ascii=False).encode(),
                           "application/json; charset=utf-8")
            elif path == "/download/xinjiang-wind/file":
                project_module(query).download_file(self, query, head=head)
            elif path == "/download/xinjiang-wind/bundle.zip":
                project_module(query).download_bundle(self, query, head=head)
            elif path == "/api/download-manifest":
                manifest, _ = downloads.selection(query)
                self.reply(200, json.dumps(manifest, ensure_ascii=False).encode(),
                           "application/json; charset=utf-8")
            elif path == "/download/file":
                downloads.download_file(self, query, head=head)
            elif path == "/download/bundle.zip":
                downloads.download_bundle(self, query, head=head)
            elif path == "/api/catalog":
                self.reply(200, json.dumps(render.catalog(), ensure_ascii=False).encode(),
                           "application/json; charset=utf-8")
            elif path == "/api/map.png":
                if query.get("values", "1") not in ("0", "1"):
                    raise ValueError("降水数值参数应为 0 或 1")
                content = render.cached_png(query.get("run", ""), int(query.get("lead", "0")),
                                            query.get("product", ""), query.get("region", ""),
                                            rain_values=query.get("values", "1") == "1")
                self.reply(200, content, "image/png", "private, max-age=86400")
            else:
                self.error(404, "未找到")
        except FileNotFoundError as error:
            self.error(404, "该时次资料尚未准备或不可用")
        except ValueError as error:
            self.error(400, str(error))
        except (BrokenPipeError, ConnectionResetError):
            pass


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8515)
    args = parser.parse_args()
    ThreadingHTTPServer(("127.0.0.1", args.port), Handler).serve_forever()
