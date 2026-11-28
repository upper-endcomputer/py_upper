# 中文维护说明：复现合并模式构建与 headless 启动采样；会生成 UpperCloseout 产物，运行时需独占工作区。
# 注释规范：docs/COMMENTING_GUIDE.md；修改行为时同步更新边界回归。
import json,os,re,shutil,subprocess,time,statistics,math
from pathlib import Path
root=Path.cwd();work=root/'.tmp/closeout';app=work/'app';app.mkdir(exist_ok=True)
shutil.copytree(root/'app/src',app/'src',dirs_exist_ok=True)
if (root/'app/resources').exists(): shutil.copytree(root/'app/resources',app/'resources',dirs_exist_ok=True)
original=(root/'app/pyproject.toml').read_text().replace('name = "Upper"','name = "UpperCloseout"')
python=root/'runtimes/linux-arm64/3.13.5/bin/python3'
results=[]
for mode in ['all','directory','none']:
 (app/'pyproject.toml').write_text(re.sub(r'^merge = ".*"$',f'merge = "{mode}"',original,flags=re.M))
 row={'mode':mode,'builds':[],'startup':{}}
 for phase in ['initial','unchanged','edited']:
  if phase=='edited':
   with (app/'src/main.py').open('a') as f:f.write('\nCLOSEOUT_BENCHMARK = 1\n')
  start=time.perf_counter()
  log=work/f'{mode}-{phase}.log'
  with log.open('w') as f: p=subprocess.run([str(python),'tools/build.py','--app',str(app)],stdout=f,stderr=subprocess.STDOUT)
  data=log.read_text();row['builds'].append({'phase':phase,'seconds':time.perf_counter()-start,'exit':p.returncode,'native_build_reports':[l for l in data.splitlines() if l.startswith(('+ merged','+ compile','Compile:'))]})
  if p.returncode:raise RuntimeError(str(log))
 exe=root/'dist/UpperCloseout/UpperCloseout'
 row['installed_bytes']=sum(p.stat().st_size for p in exe.parent.rglob('*') if p.is_file())
 cache=work/'bench-cache'
 for kind in ['cold','warm']:
  values=[]
  for i in range(5):
   if kind=='cold' and cache.exists():shutil.rmtree(cache)
   env=dict(os.environ,APPBUILD_EGG_CACHE=str(cache),APPBUILD_HEADLESS='1',APPBUILD_NO_DIALOG='1',APPBUILD_STARTUP_TIMING='1')
   start=time.perf_counter();p=subprocess.run([str(exe)],env=env,capture_output=True,text=True,timeout=60);values.append(time.perf_counter()-start)
   (work/f'{mode}-{kind}-{i}.log').write_text(p.stdout+p.stderr)
   if p.returncode or 'GUI OK' not in p.stdout: raise RuntimeError(f'{mode} {kind}: {p.stdout} {p.stderr}')
  row['startup'][kind]={'seconds':values,'median':statistics.median(values),'p95':sorted(values)[math.ceil(len(values)*.95)-1]}
 results.append(row);(work/'benchmark.json').write_text(json.dumps(results,indent=2));print(mode,'complete',flush=True)
 shutil.copyfile(root/'app/src/main.py',app/'src/main.py')
