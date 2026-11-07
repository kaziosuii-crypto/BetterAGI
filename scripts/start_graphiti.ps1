# 启动本机 Graphiti 依赖（Neo4j），可重复执行。
# 用法: powershell -ExecutionPolicy Bypass -File .\scripts\start_graphiti.ps1
$ErrorActionPreference = "Stop"

Write-Host "[1/2] 启动 Neo4j 容器..."
wsl -d Ubuntu -- bash -lc "docker start graphiti-neo4j >/dev/null 2>&1 || docker run -d --name graphiti-neo4j -p 7474:7474 -p 7687:7687 -e NEO4J_AUTH=neo4j/graphiti2026 neo4j:5.26 >/dev/null"

Write-Host "[2/2] 探活 bolt://localhost:7687 ..."
$ok = $false
for ($i = 0; $i -lt 40; $i++) {
    if (Test-NetConnection -ComputerName 127.0.0.1 -Port 7687 -InformationLevel Quiet -WarningAction SilentlyContinue) { $ok = $true; break }
    Start-Sleep -Milliseconds 1500
}

if ($ok) {
    Write-Host "Neo4j 就绪: bolt://localhost:7687 (user=neo4j, password=graphiti2026)"
    Write-Host "提醒: WSL 空闲会自动停机并带走容器, 长期运行需保活一个 wsl 会话."
} else {
    Write-Host "Neo4j 未就绪, 检查: wsl -d Ubuntu -- docker logs --tail 50 graphiti-neo4j" -ForegroundColor Red
    exit 1
}