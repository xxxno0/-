"""실행:
  python -m lolcoach                 코치 서버 시작 (폰/PC 브라우저로 접속)
  python -m lolcoach learn [챔피언]   챌린저·프로 경기 학습 (예: learn Ahri)
  python -m lolcoach bench 챔피언     학습된 합의 통계 보기
  python -m lolcoach mock            롤 없이 테스트용 가짜 게임 서버 실행
"""
from __future__ import annotations

import argparse
import datetime
import ipaddress
import socket
import sys

from .config import DATA_DIR, Config


def lan_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))     # 실제로 보내지 않음, 내 공유기 쪽 IP 확인용
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return "127.0.0.1"


def ensure_cert(ip: str, name: str = "cert") -> tuple[str, str]:
    """폰 브라우저 마이크용 자체 서명 HTTPS 인증서 (처음 한 번 생성)"""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    cert, key = DATA_DIR / f"{name}.pem", DATA_DIR / f"{name}-key.pem"
    marker = DATA_DIR / f"{name}-ip.txt"
    if cert.exists() and key.exists() and marker.exists() and marker.read_text() == ip:
        return str(cert), str(key)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    k = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "lolcoach")])
    now = datetime.datetime.now(datetime.timezone.utc)
    c = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(k.public_key())
         .serial_number(x509.random_serial_number())
         .not_valid_before(now - datetime.timedelta(days=1)).not_valid_after(now + datetime.timedelta(days=3650))
         .add_extension(x509.SubjectAlternativeName([
             x509.DNSName("localhost"), x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
             x509.IPAddress(ipaddress.ip_address(ip))]), critical=False)
         .sign(k, hashes.SHA256()))
    cert.write_bytes(c.public_bytes(serialization.Encoding.PEM))
    key.write_bytes(k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
                                    serialization.NoEncryption()))
    marker.write_text(ip)
    return str(cert), str(key)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="lolcoach")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("serve")
    lp = sub.add_parser("learn")
    lp.add_argument("champion", nargs="?")
    lp.add_argument("--max-calls", type=int, default=600)
    bp = sub.add_parser("bench")
    bp.add_argument("champion")
    bp.add_argument("--position")
    bp.add_argument("--vs")
    mp = sub.add_parser("mock")
    mp.add_argument("--speed", type=float, default=10.0, help="게임 시간 배속")
    args = ap.parse_args(argv)
    cfg = Config.load()
    if not (DATA_DIR / "config.json").exists():
        cfg.save()

    if args.cmd == "learn":
        from .learn import Brain, crawl
        from .riot import Riot
        crawl(Riot(cfg.riot_api_key, cfg.platform, cfg.region), Brain(), pro_accounts=cfg.pro_accounts,
              champion=args.champion, max_calls=args.max_calls)
        return
    if args.cmd == "bench":
        from .learn import Brain, benchmark, describe
        from .riot import DataDragon
        dd = DataDragon()
        try:
            dd.load()
        except OSError:
            dd = None
        print(describe(benchmark(Brain(), args.champion, args.position, top_n=cfg.top_players_per_champion,
                                 min_games=cfg.min_games_on_champion, opp_champion=args.vs, dd=dd)))
        return
    if args.cmd == "mock":
        from .mock import run_mock
        run_mock(speed=args.speed)
        return

    import uvicorn

    from .server import create_app
    ip = lan_ip()
    ssl_args = {}
    scheme = "http"
    if cfg.https:
        cert, key = ensure_cert(ip)
        ssl_args = {"ssl_certfile": cert, "ssl_keyfile": key}
        scheme = "https"
    print(f"\n  롤 코치 실행 중\n  PC에서:  {scheme}://localhost:{cfg.port}\n  폰에서:  {scheme}://{ip}:{cfg.port}"
          f"  (같은 와이파이, 처음 접속 시 '안전하지 않음' 경고 → 고급 → 계속)\n  설정 파일: {DATA_DIR / 'config.json'}\n")
    uvicorn.run(create_app(cfg), host=cfg.host, port=cfg.port, log_level="warning", **ssl_args)


if __name__ == "__main__":
    main(sys.argv[1:])
