"""Record a captioned walkthrough of the CarbonLedger dashboard (Playwright + Edge)."""
import shutil
import subprocess
import sys
from pathlib import Path

import imageio_ffmpeg
from playwright.sync_api import sync_playwright

URL = "http://localhost:8611"
OUT = Path(sys.argv[1])
TMP = OUT.parent / "_video_tmp"
W, H = 1440, 900

OVERLAY_JS = """
([kind, title, sub]) => {
  let el = document.getElementById('cf-cap');
  if (!el) { el = document.createElement('div'); el.id = 'cf-cap'; document.body.appendChild(el); }
  if (kind === 'none') { el.style.display = 'none'; return; }
  el.style.display = 'flex';
  if (kind === 'card') {
    el.style.cssText = 'display:flex;position:fixed;inset:0;z-index:2147483647;background:linear-gradient(135deg,#0f2e3d,#0f5a54);' +
      'color:#fff;flex-direction:column;align-items:center;justify-content:center;text-align:center;padding:60px;' +
      'font-family:Segoe UI,Arial,sans-serif;';
    el.innerHTML = '<div style="font-size:52px;font-weight:700;margin-bottom:22px">' + title + '</div>' +
                   '<div style="font-size:26px;line-height:1.5;max-width:1100px;opacity:.92">' + sub + '</div>';
  } else {
    el.style.cssText = 'display:block;position:fixed;left:50%;bottom:28px;transform:translateX(-50%);z-index:2147483647;' +
      'background:rgba(15,23,42,.88);color:#fff;padding:16px 26px;border-radius:12px;max-width:1150px;' +
      'font-family:Segoe UI,Arial,sans-serif;box-shadow:0 8px 30px rgba(0,0,0,.35);';
    el.innerHTML = '<div style="font-size:15px;letter-spacing:.08em;text-transform:uppercase;color:#5eead4;margin-bottom:6px">' +
                   title + '</div><div style="font-size:22px;line-height:1.45">' + sub + '</div>';
  }
}
"""


def main():
    TMP.mkdir(exist_ok=True)
    with sync_playwright() as p:
        b = p.chromium.launch(channel="msedge")
        ctx = b.new_context(viewport={"width": W, "height": H}, record_video_dir=str(TMP),
                            record_video_size={"width": W, "height": H})
        page = ctx.new_page()
        page.goto(URL)
        page.get_by_text("Signed in as").wait_for(timeout=120_000)
        page.wait_for_timeout(1500)

        def cap(kind, title="", sub="", hold=0):
            page.evaluate(OVERLAY_JS, [kind, title, sub])
            if hold:
                page.wait_for_timeout(hold)

        def tab(name):
            page.get_by_role("tab", name=name).click()
            page.wait_for_timeout(1500)

        cap("card", "CarbonLedger",
            "A carbon + cost decision platform for built-environment decarbonisation"
            "<br><br><span style='font-size:22px'>A working demonstrator by <b>Waqas Ahmad</b><br>"
            "prepared for the Northumbria University × Identity Consult KTP: Software and Analytics Specialist"
            "<br><br>All data synthetic · factors illustrative</span>", 6500)

        cap("bar", "Portfolio · consultant view",
            "Four fictional clients, 38 buildings. Client meter data, consultancy cost rates and open carbon factors "
            "sit in one warehouse, and two bad records were quarantined at ingestion.", 7500)
        page.mouse.wheel(0, 700)
        cap("bar", "Analytics",
            "A robust z-score against peers of the same building type flags three sites with implausible "
            "electricity use: a metering fault or a real waste opportunity, for a person to decide.", 7000)
        page.mouse.wheel(0, -700)

        tab("Abatement curve")
        cap("bar", "Whole-life carbon-cost appraisal",
            "Six measures applied fabric first. Bar width = net whole-life tCO₂e (operational minus embodied); "
            "height = lifetime £ per tonne. <span style='color:#6ee7b7'>Green pays for itself</span>.", 8500)
        cap("bar", "The trade-offs a client has to see",
            "PV saves money but displaces a cleaner and cleaner grid. Heat pumps raise running cost at today's "
            "prices, yet deliver half the carbon.", 8000)

        tab("Investment plan")
        cap("bar", "Where should the next pound go?",
            "An exact MILP picks one package per building to maximise whole-life tCO₂e within budget: "
            "<b>£3m buys ~11,900 t with a +£4.6m NPV</b>.", 8000)
        page.get_by_text("Grant-eligible only").click()
        cap("bar", "Constraints",
            "Restrict to grant-eligible packages (PSDS/Salix-style £/t cap), or require the portfolio not to lose "
            "money. The plan re-optimises instantly.", 7000)
        page.get_by_text("Grant-eligible only").click()
        page.wait_for_timeout(800)

        tab("Design options")
        cap("bar", "Design options · RICS whole-life carbon modules",
            "Steel, low-carbon concrete and timber frames compared by module A1-A5. Timber cuts upfront carbon "
            "15%, at about £4,350 per tonne avoided, on the same £/t scale as retrofit.", 9000)

        page.get_by_text("Client: Wearside Primary Care Partnership").click()
        page.wait_for_timeout(2500)
        tab("Data & governance")
        cap("bar", "Security · signed in as a client",
            "This session only contains the client's own buildings. Unit rates and other clients' data are not "
            "filtered out, they are simply not there.", 7500)
        box = page.get_by_label("SQL (read-only, guarded)")
        box.fill("SELECT * FROM quarantine")
        box.press("Enter")
        page.wait_for_timeout(1500)
        cap("bar", "Security · guarded SQL",
            "Out-of-scope tables, writes, file access and multi-statement queries are blocked, and every attempt "
            "is written to the audit log.", 7000)

        tab("Ask the data")
        q = page.get_by_label("Question")
        q.fill("Plan a £0.5m budget")
        q.press("Enter")
        page.get_by_text("Best plan for").wait_for(timeout=60_000)
        page.wait_for_timeout(1000)
        cap("bar", "AI analyst",
            "Claude with two tools, guarded SQL and the optimiser, answering inside the user's scope "
            "(shown here in offline mode). It cannot reach data the session doesn't hold.", 8500)

        cap("card", "github.com/malikwaqas077/carbonledger",
            "34 automated tests · CI · Streamlit dashboard<br><br>"
            "<span style='font-size:22px'>Waqas Ahmad · malikwaqas077@gmail.com</span>", 6000)
        video = page.video
        ctx.close()
        src = Path(video.path())
        b.close()

    ff = imageio_ffmpeg.get_ffmpeg_exe()
    subprocess.run([ff, "-y", "-loglevel", "error", "-i", str(src), "-c:v", "libx264", "-pix_fmt", "yuv420p",
                    "-crf", "22", "-preset", "medium", "-movflags", "+faststart", str(OUT)], check=True)
    shutil.rmtree(TMP, ignore_errors=True)
    print("wrote", OUT, OUT.stat().st_size // 1024, "KB")


if __name__ == "__main__":
    main()
