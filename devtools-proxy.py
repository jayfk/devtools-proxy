#!/usr/bin/env python3

import asyncio
import json
import re
import sys

import aiohttp
from aiohttp.web import Application, MsgType, Response, WebSocketResponse

from monkey_patch import patch_create_server

# When you're too lazy to use argparse, or your tool on «It's just a prototype» stage
if len(sys.argv) >= 2:
    if ':' in sys.argv[1]:
        PROXY_DEBUGGING_HOST, ports = sys.argv[1].split(';')
        PROXY_DEBUGGING_PORTS = [int(port) for port in ports.split(',')]
    else:
        PROXY_DEBUGGING_HOST = '127.0.0.1'
        PROXY_DEBUGGING_PORTS = [int(port) for port in sys.argv[1].split(',')]
else:
    PROXY_DEBUGGING_HOST = '127.0.0.1'
    PROXY_DEBUGGING_PORTS = [9222]

CHROME_DEBUGGING_HOST = '127.0.0.1'
CHROME_DEBUGGING_PORT = int(sys.argv[2]) if len(sys.argv) >= 3 else 12222
DEVTOOLS_PATTERN = re.compile(r"(127\.0\.0\.1|localhost|%s):%d/" % (CHROME_DEBUGGING_HOST, CHROME_DEBUGGING_PORT))


# def print(*args, **kwargs):
#     pass

async def the_handler(request):
    response = WebSocketResponse()

    ok, _protocol = response.can_prepare(request)
    return await (ws_handler(request) if ok else proxy_handler(request))


async def ws_handler(request):
    app = request.app
    tab_id = request.path_qs.split('/')[-1]
    tabs = app['tabs']

    if tabs.get(tab_id) is None:
        app['tabs'][tab_id] = {}
        # https://github.com/KeepSafe/aiohttp/commit/8d9ee8b77820a2af11d7c1716f793a610afe306f
        task = app.loop.create_task(ws_browser_handler(request))
        app['tasks'].append(task)

    return await ws_client_handler(request)


async def ws_client_handler(request):
    unprocessed_msg = None
    app = request.app
    path_qs = request.path_qs
    tab_id = path_qs.split('/')[-1]
    url = "ws://%s:%d%s" % (CHROME_DEBUGGING_HOST, CHROME_DEBUGGING_PORT, path_qs)

    ws_client = WebSocketResponse()
    await ws_client.prepare(request)

    client_id = len(app['clients']) + 1
    app['clients'][ws_client] = {
        'id': client_id,
        'tab_id': tab_id
    }

    log_prefix = "[CLIENT %d]" % client_id

    print(log_prefix, 'CONNECTED')

    if app['tabs'][tab_id].get('ws') is None or app['tabs'][tab_id]['ws'].closed:
        session = aiohttp.ClientSession(loop=app.loop)
        app['sessions'].append(session)
        # TODO: handle connection to non-existing tab (in case of typo in tab_id, or try to connect to old one)
        app['tabs'][tab_id]['ws'] = await session.ws_connect(url, autoclose=False, autoping=False)

    while True:
        if unprocessed_msg:
            data = unprocessed_msg.data
            print(log_prefix, '>>', data)
            app['tabs'][tab_id]['ws'].send_str(data)
            unprocessed_msg = None

        async for msg in ws_client:
            if msg.tp == MsgType.text:
                app['tabs'][tab_id]['active_client'] = ws_client
                if app['tabs'][tab_id]['ws'].closed:
                    unprocessed_msg = msg
                    print(log_prefix, 'RECONNECTED')
                    break
                data = msg.data
                print(log_prefix, '>>', data)
                app['tabs'][tab_id]['ws'].send_str(data)
            else:
                print(log_prefix, 'DISCONNECTED')
                return ws_client


async def ws_browser_handler(request):
    log_prefix = '<<'
    app = request.app
    tab_id = request.path_qs.split('/')[-1]

    while True:
        while app['tabs'][tab_id].get('ws') is None or app['tabs'][tab_id]['ws'].closed:
            await asyncio.sleep(0.1)
            print("[BROWSER %s]" % tab_id, 'WAITED')
        else:
            print("[BROWSER %s]" % tab_id, 'CONNECTED')

        async for msg in app['tabs'][tab_id]['ws']:
            if msg.tp == MsgType.text:
                if json.loads(msg.data).get('id') is None:
                    clients = {k: v for k, v in app['clients'].items() if v.get('tab_id') == tab_id}
                    for client in clients.keys():
                        if not client.closed:
                            client_id = app['clients'][client]['id']
                            print('[CLIENT %d]' % client_id, log_prefix, msg.data)
                            client.send_str(msg.data)
                else:
                    client_id = app['clients'][app['tabs'][tab_id]['active_client']]['id']
                    print('[CLIENT %d]' % client_id, log_prefix, msg.data)
                    app['tabs'][tab_id]['active_client'].send_str(msg.data)
            else:
                print("[BROWSER %s]" % tab_id, 'DISCONNECTED')
                break


async def proxy_handler(request):
    path_qs = request.path_qs
    session = aiohttp.ClientSession(loop=request.app.loop)
    url = "http://%s:%s%s" % (CHROME_DEBUGGING_HOST, CHROME_DEBUGGING_PORT, path_qs)

    print("[HTTP] %s" % path_qs)
    try:
        if request.path in ['/json', '/json/list']:
            response = await session.get(url)
            data = await response.json()
            proxy_debugging_port = int(request.host.split(':')[1])
            for tab in data:
                for k, v in tab.items():
                    if ":%d/" % CHROME_DEBUGGING_PORT in v:
                        tab[k] = DEVTOOLS_PATTERN.sub("%s:%d/" % (PROXY_DEBUGGING_HOST, proxy_debugging_port), tab[k])

                if tab.get('id') is None:
                    print('[WARN]', "Got a tab without id (which is improbable): %s" % tab)
                    continue

                devtools_url = "%s:%d/devtools/page/%s" % (PROXY_DEBUGGING_HOST, proxy_debugging_port, tab['id'])
                if tab.get('webSocketDebuggerUrl') is None:
                    tab['webSocketDebuggerUrl'] = "ws://%s" % devtools_url
                if tab.get('devtoolsFrontendUrl') is None:
                    tab['devtoolsFrontendUrl'] = "/devtools/inspector.html?ws=%s" % devtools_url

            return Response(status=response.status, body=json.dumps(data).encode('utf-8'))
        else:
            return await transparent_request(session, url)
    except (aiohttp.errors.ClientOSError, aiohttp.errors.ClientResponseError) as exc:
        return aiohttp.web.HTTPBadGateway(text=str(exc))
    finally:
        session.close()


async def transparent_request(session, url):
    response = await session.get(url)
    content_type = response.headers[aiohttp.hdrs.CONTENT_TYPE].split(';')[0]  # Could be 'text/html; charset=UTF-8'
    return Response(status=response.status, body=await response.read(), content_type=content_type)


async def init(loop):
    app = Application(loop=loop)
    app['clients'] = {}
    app['tabs'] = {}
    # TODO: Move session and task handling to proper places
    app['sessions'] = []
    app['tasks'] = []

    app.router.add_route('*', '/{path:.*}', the_handler)

    handler = app.make_handler()
    srv = await loop.create_server(handler, PROXY_DEBUGGING_HOST, PROXY_DEBUGGING_PORTS)
    print(
        "Server started at %s:%s\n"
        "Use --remote-debugging-port=%d for Chrome" % (
            PROXY_DEBUGGING_HOST, PROXY_DEBUGGING_PORTS, CHROME_DEBUGGING_PORT
        )
    )
    return app, srv, handler


async def finish(app, srv, handler):
    for ws in list(app['clients'].keys()) + [tab['ws'] for tab in app['tabs'].values() if tab.get('ws') is not None]:
        if not ws.closed:
            await ws.close()

    for session in app['sessions']:
        if not session.closed:
            await session.close()

    for task in app['tasks']:
        task.cancel()

    await asyncio.sleep(0.1)
    srv.close()
    await handler.finish_connections()
    await srv.wait_closed()


if __name__ == '__main__':
    patch_create_server()

    loop = asyncio.get_event_loop()
    application, srv, handler = loop.run_until_complete(init(loop))
    try:
        loop.run_forever()
    except KeyboardInterrupt:
        loop.run_until_complete(finish(application, srv, handler))
