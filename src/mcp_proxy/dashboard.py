"""Minimal HTML dashboard for MCP proxy server monitoring."""

from starlette.requests import Request
from starlette.responses import HTMLResponse
from starlette.routing import Route

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>MCP Proxy Dashboard</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:system-ui,-apple-system,sans-serif;background:#1a1a2e;color:#e0e0e0;padding:20px}
h1{color:#00d4ff;margin-bottom:20px;font-size:1.5rem}
.meta{color:#888;font-size:0.85rem;margin-bottom:16px}
table{width:100%;border-collapse:collapse;background:#16213e;border-radius:8px;overflow:hidden}
th{background:#0f3460;padding:12px 16px;text-align:left;font-size:0.85rem;text-transform:uppercase;color:#00d4ff}
td{padding:10px 16px;border-top:1px solid #1a1a2e;font-size:0.9rem}
tr:hover{background:#1a2744}
.status{display:inline-block;padding:2px 8px;border-radius:4px;font-size:0.8rem;font-weight:600}
.status-running{background:#0a3d0a;color:#4caf50}
.status-failed{background:#3d0a0a;color:#f44336}
.status-circuit-open{background:#3d2a0a;color:#ff9800}
.error{color:#f44336}
#loading{color:#888;font-style:italic}
</style>
</head>
<body>
<h1>&#9881; MCP Proxy Dashboard</h1>
<div class="meta">Auto-refresh: 5s | <span id="last-update">loading...</span></div>
<div id="content"><p id="loading">Loading status...</p></div>
<script>
function esc(s){return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');}
async function refresh(){
  try{
    const r=await fetch('/status');
    const d=await r.json();
    const servers=d.server_instances||{};
    const names=Object.keys(servers);
    let html='<table><tr><th>Server</th><th>Status</th><th>Command</th></tr>';
    if(names.length===0){
      html+='<tr><td colspan="3">No servers configured</td></tr>';
    }else{
      for(const name of names){
        const s=servers[name];
        const st=s.status||'unknown';
        let cls='status-'+esc(st);
        if(st==='circuit-open')cls='status-circuit-open';
        html+=`<tr><td><b>${esc(name)}</b></td><td><span class="status ${cls}">${esc(st)}</span></td><td>${esc(s.command||'-')}</td></tr>`;
      }
    }
    html+='</table>';
    html+=`<p class="meta" style="margin-top:12px">Healthy: ${esc(d.servers_running)}/${esc(d.servers_total)} | Last activity: ${esc(d.api_last_activity||'-')}</p>`;
    document.getElementById('content').innerHTML=html;
    document.getElementById('last-update').textContent=new Date().toLocaleTimeString();
  }catch(e){
    document.getElementById('content').innerHTML='<p class="error">Failed to fetch status: '+esc(e.message)+'</p>';
  }
}
refresh();
setInterval(refresh,5000);
</script>
</body>
</html>"""


async def handle_dashboard(_: Request) -> HTMLResponse:
    """Serve the dashboard HTML page."""
    return HTMLResponse(DASHBOARD_HTML)


def create_dashboard_route() -> list[Route]:
    """Create the dashboard route."""
    return [Route("/dashboard", endpoint=handle_dashboard, methods=["GET"])]
