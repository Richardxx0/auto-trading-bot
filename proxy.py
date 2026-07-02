# HTTP CONNECT proxy for Binance API
import socket, select, os, signal
PORT = int(os.getenv("PROXY_PORT", "18080"))
HOST = os.getenv("PROXY_HOST", "0.0.0.0")
logfile = os.path.join(os.path.dirname(os.path.abspath(__file__)), "proxy.log")
def log(m):
    with open(logfile, "a", encoding="utf-8") as f:
        f.write(m + "\n")
def handle(conn, addr):
    try:
        data = conn.recv(4096)
        if not data: return
        host, port = data.split(b"\r\n")[0].split()[1].decode().split(":")
        port = int(port)
        log("CONNECT %s:%d from %s" % (host, port, addr[0]))
        target = socket.create_connection((host, port), timeout=15)
        conn.sendall(b"HTTP/1.1 200 OK\r\n\r\n")
        conn.setblocking(False); target.setblocking(False)
        while True:
            r, _, _ = select.select([conn, target], [], [], 60)
            if not r: break
            for s in r:
                d = s.recv(32768)
                if not d: raise ConnectionError
                (target if s is conn else conn).sendall(d)
    except Exception as e:
        log("%s:%d -> %s:%d %s" % (addr[0], addr[1], host, port, e))
    finally:
        conn.close()
srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
srv.bind((HOST, PORT))
srv.listen(10)
log("Proxy on %s:%d PID=%d" % (HOST, PORT, os.getpid()))
while True:
    c, a = srv.accept()
    handle(c, a)
