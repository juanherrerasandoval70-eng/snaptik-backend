import subprocess
import json

req = json.dumps({"url": "https://www.tiktok.com/@tiktok/video/7106594312292453675"})
with open(r"C:\Users\Administrator\.gemini\antigravity\scratch\snaptik-backend\test_req.json", "w", encoding="utf-8") as f:
    f.write(req)

cmd = (
    'adb connect 192.168.101.74:5555 && '
    'adb push "C:\\Users\\Administrator\\.gemini\\antigravity\\scratch\\snaptik-backend\\test_req.json" /data/local/tmp/test_req.json && '
    'adb shell "curl -s -X POST http://192.168.101.84:8000/api/extract -H \'Content-Type: application/json\' -d @/data/local/tmp/test_req.json" && '
    'adb shell "rm /data/local/tmp/test_req.json"'
)

res = subprocess.run(cmd, shell=True, capture_output=True, text=True)
print("STDOUT:", res.stdout)
print("STDERR:", res.stderr)
