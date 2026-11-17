"""Local management UI and a filtered listener for Tailscale Funnel."""
import argparse
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time

from werkzeug.serving import make_server
from werkzeug.wrappers import Response

from server import app, load_db, valid_http_url

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TAILSCALE_URL = 'https://msi.taild1ec7e.ts.net'
PUBLIC_ORIGIN = 'http://127.0.0.1:8001'
PUBLIC_PATH = re.compile(r'/l/[a-z0-9_-]+(?:/pixel\.png)?\Z')
SUBMIT_PATH = re.compile(r'/(?:collect|beacon)/[a-z0-9_-]+\Z')


class PublicLinksOnly:
    """Deny management endpoints regardless of proxy headers or URL queries."""
    def __init__(self, application):
        self.application = application

    def __call__(self, environ, start_response):
        path = environ.get('PATH_INFO', '')
        method = environ.get('REQUEST_METHOD', '')
        allowed = ((method in ('GET', 'HEAD') and PUBLIC_PATH.fullmatch(path))
                   or (method == 'POST' and SUBMIT_PATH.fullmatch(path)))
        if not allowed:
            return Response('Pagina non disponibile.', status=404)(environ, start_response)
        return self.application(environ, start_response)


class SerializedRequests:
    """Keep JSON read/modify/write transactions serialized across both listeners."""
    def __init__(self, application, lock):
        self.application, self.lock = application, lock

    def __call__(self, environ, start_response):
        with self.lock:
            return self.application(environ, start_response)


def find_cloudflared():
    local = os.path.join(BASE_DIR, 'cloudflared.exe' if os.name == 'nt' else 'cloudflared')
    if os.path.isfile(local):
        return local
    return shutil.which('cloudflared')


def cloudflare_configuration(executable, environ):
    """Keep the previous Cloudflare configuration available for emergencies."""
    fixed_url = environ.get('GET_PUBLIC_URL', '').strip().rstrip('/')
    token = environ.get('TUNNEL_TOKEN', '').strip()
    if fixed_url or token:
        if not fixed_url or not token:
            raise ValueError('Per un indirizzo fisso imposta GET_PUBLIC_URL e TUNNEL_TOKEN. Vedi AVVIO.txt.')
        if not fixed_url.startswith('https://') or not valid_http_url(fixed_url, base=True):
            raise ValueError('GET_PUBLIC_URL deve contenere un indirizzo HTTPS completo, senza percorsi.')
        if fixed_url == TAILSCALE_URL:
            raise ValueError('GET_PUBLIC_URL deve essere il dominio Cloudflare di emergenza.')
        if '.trycloudflare.com' in fixed_url:
            raise ValueError('Un indirizzo temporaneo trycloudflare.com non puo diventare fisso.')
        return [executable, 'tunnel', '--no-autoupdate', 'run'], fixed_url
    return [executable, 'tunnel', '--url', PUBLIC_ORIGIN, '--no-autoupdate'], None


def announce(url, provider):
    print(f'\nIndirizzo pubblico ({provider}): {url}', flush=True)
    print('Pannello privato: http://127.0.0.1:8000', flush=True)
    print('Condividi il link completo /l/ID. Ctrl+C arresta il server.', flush=True)


def main(cloudflare_fallback=False):
    if cloudflare_fallback:
        executable = find_cloudflared()
        if not executable:
            print('Manca cloudflared: https://developers.cloudflare.com/tunnel/downloads/')
            return 1
        try:
            command, fixed_url = cloudflare_configuration(executable, os.environ)
        except ValueError as error:
            print(error)
            return 1

    servers, threads, process = [], [], None
    app.config['PUBLIC_TUNNEL_URL'] = TAILSCALE_URL
    app.config['MAX_CONTENT_LENGTH'] = 128 * 1024
    try:
        load_db()
        lock = threading.RLock()
        shared = SerializedRequests(app, lock)
        # Both tunnels point to the filtered listener on port 8001.
        for port, application in ((8000, shared), (8001, PublicLinksOnly(shared))):
            servers.append(make_server('127.0.0.1', port, application, threaded=True))
        for listener in servers:
            thread = threading.Thread(target=listener.serve_forever, daemon=True)
            thread.start()
            threads.append(thread)
        if not cloudflare_fallback:
            announce(TAILSCALE_URL, 'Tailscale Funnel')
            while True:
                time.sleep(1)

        process = subprocess.Popen(
            command,
            cwd=BASE_DIR, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding='utf-8', errors='replace',
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        lines = queue.Queue()

        def read_output():
            for line in process.stdout:
                lines.put(line.rstrip())
            lines.put(None)

        threading.Thread(target=read_output, daemon=True).start()
        print('Avvio di Cloudflare in modalita di emergenza...', flush=True)
        deadline = time.monotonic() + 90
        announced = False
        recent = []
        while True:
            if not announced and time.monotonic() > deadline:
                raise RuntimeError('Il tunnel non ha restituito un indirizzo entro 90 secondi.')
            try:
                line = lines.get(timeout=0.5)
            except queue.Empty:
                continue
            if line is None:
                raise RuntimeError('Il tunnel si e chiuso. Ultimi messaggi: ' + '\n'.join(recent))
            # Never echo the tunnel credential in diagnostics.
            token = os.environ.get('TUNNEL_TOKEN', '')
            safe_line = line.replace(token, '[token]') if token else line
            recent = (recent + [safe_line])[-6:]
            match = re.search(r'https://[a-z0-9-]+\.trycloudflare\.com\b', line)
            ready_fixed = fixed_url and 'Registered tunnel connection' in line
            if (ready_fixed or (match and not fixed_url)) and not announced:
                url = fixed_url if ready_fixed else match.group(0)
                app.config['PUBLIC_TUNNEL_URL'] = url
                announced = True
                announce(url, 'Cloudflare, emergenza')
                print('Per tornare a Tailscale, arresta e riavvia senza --fallback-cloudflare.', flush=True)
    except KeyboardInterrupt:
        print('\nArresto del collegamento pubblico.')
        return 0
    except (OSError, RuntimeError) as error:
        print('Avvio pubblico non riuscito:', error)
        return 1
    finally:
        app.config['PUBLIC_TUNNEL_URL'] = None
        if process and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        for listener, thread in zip(servers, threads):
            listener.shutdown()
            thread.join(timeout=2)
        for listener in servers:
            listener.server_close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Avvia il pannello e i link pubblici.')
    parser.add_argument('--fallback-cloudflare', action='store_true',
                        help='Avvia Cloudflare manualmente in caso di guasto prolungato del Funnel.')
    sys.exit(main(parser.parse_args().fallback_cloudflare))
