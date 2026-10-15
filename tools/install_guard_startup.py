"""Install the live-host block at normal interpreter startup."""
from pathlib import Path
import sysconfig
repo = Path(__file__).resolve().parents[1]
site = Path(sysconfig.get_path("purelib"))
startup = 'import os; exec("try:\\n import orbquant_network_guard; orbquant_network_guard.install()\\nexcept BaseException:\\n os.write(2, b\'OrbQuant network guard failed; stopping Python before network use\\\\n\'); os._exit(78)")\n'
(site/"zzz_orbquant_network_guard.pth").write_text(str(repo/"src")+"\n"+startup,encoding="utf-8")
print("Installed fail-closed live-host block; restart existing Python processes.")
