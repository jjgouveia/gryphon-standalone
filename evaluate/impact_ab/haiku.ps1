$root = "C:\Users\gouvi\Documents\Desenvolvimento\gryphon"
Set-Location $root
$env:PYTHONPATH = $root
$py = "$root\.venv\Scripts\python.exe"
$m = "claude-haiku-4-5-20251001"
& $py "$root\evaluate\impact_ab\run_depth.py" run --reps 2 --jobs 3 --model $m *> "$root\evaluate\impact_ab\haiku_depth.out"
& $py "$root\evaluate\impact_ab\run.py" run --reps 1 --jobs 3 --model $m *> "$root\evaluate\impact_ab\haiku_direct.out"
"done" | Out-File "$root\evaluate\impact_ab\haiku_done.txt"
