# SmartTech Support Agent (foundation release)

Not the full application yet - see PROJECT_STATUS.md for exactly what exists.

## Run (Windows PowerShell, from D:\smarttech-support-agent)
1. `.\scripts\setup.ps1`
2. Edit `backend\.env` (database password, JWT secret).
3. `.\scripts\verify.ps1`
4. `.\scripts\start.ps1` then open http://localhost:8000/docs
