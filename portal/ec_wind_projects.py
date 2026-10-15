"""Read-only exports of the EC data used by the Balikun verification site."""
from datetime import date
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlencode
import hashlib,json,os,re,zipfile
import downloads,wind_projects

ROOT=Path(os.getenv('EC_WIND_PROJECT_ROOT','/home/chengyang/ec-gfs-server/data/xinjiang-wind-ec'))
FILES={'hourly':{'zip':'forecasts.zip','native':'native-grid.npz','manifest':'manifest.json'},
       '15min':{'zip':'forecasts-15min.zip','manifest':'manifest.json'}}

def variant(interval):
    if interval not in FILES:raise ValueError('时间分辨率须为hourly或15min')
    return ROOT/interval,FILES[interval]

@lru_cache(maxsize=2)
def _catalog(path,mtime_ns,size):return json.loads(Path(path).read_text())

def catalog(interval='hourly'):
    root,files=variant(interval);path=root/'catalog.json';stat=path.stat()
    data=_catalog(str(path),stat.st_mtime_ns,stat.st_size)
    # Do not mutate the cached catalog while preparing request-specific URLs.
    out=dict(data);out['days']=[]
    for day in data['days']:
        item=dict(day);item['files']={kind:{'bytes':f['bytes'],'sha256':f['sha256'],
            'url':'file?'+urlencode({'model':'EC','interval':interval,'day':day['day'],'kind':kind})}
            for kind,f in day['files'].items()}
        out['days'].append(item)
    return out

def day_record(day,kind,interval='hourly',data=None):
    root,files=variant(interval)
    if kind not in files or not re.fullmatch(r'\d{4}-\d{2}-\d{2}',day or ''):raise ValueError('日期或文件类型无效')
    date.fromisoformat(day);data=data or catalog(interval)
    item=next((d for d in data['days'] if d['day']==day),None)
    if not item or item['status']!='complete':raise FileNotFoundError('该日资料尚未发布')
    base=(root/'days'/day).resolve();path=(base/files[kind]).resolve()
    if not path.is_relative_to(base) or not path.is_file():raise FileNotFoundError('项目文件不可用')
    stat=path.stat()
    if stat.st_size!=item['files'][kind]['bytes']:raise ValueError('项目文件已变化，请刷新目录')
    return {'name':f'ec_balikun3_{day}_100m_'+files[kind], 'path':path,'bytes':stat.st_size,
            'mtime_ns':stat.st_mtime_ns,'sha256':item['files'][kind]['sha256']}

def download_file(handler,query,head=False):
    record=day_record(query.get('day',''),query.get('kind','zip'),query.get('interval','hourly'))
    size=record['bytes'];start,end,status=0,size-1,200;etag='"'+record['sha256']+'"'
    header=None if head else handler.headers.get('Range')
    if handler.headers.get('If-Range',etag)!=etag:header=None
    if header:
        match=re.fullmatch(r'bytes=(\d*)-(\d*)',header)
        if not match or not any(match.groups()):start=size
        else:
            first,last=match.groups()
            if first:start,end=int(first),min(int(last),end) if last else end
            elif int(last)>0:start=max(0,size-int(last))
            else:start=size
        if start>=size or end<start:
            handler.send_response(416);handler.send_header('Content-Range',f'bytes */{size}');handler.send_header('Content-Length','0');handler.end_headers();return
        status=206
    with downloads.open_snapshot(record) as stream:
        if handler.headers.get('If-None-Match')==etag:
            handler.send_response(304);handler.send_header('ETag',etag);handler.end_headers();return
        kind={'zip':'application/zip','manifest':'application/json; charset=utf-8','native':'application/octet-stream'}[query.get('kind','zip')]
        handler.send_response(status)
        for name,value in [('Content-Type',kind),('Content-Length',str(end-start+1)),
                           ('Content-Disposition',f'attachment; filename="{record["name"]}"'),('Cache-Control','private, no-store'),
                           ('ETag',etag),('Accept-Ranges','bytes'),('X-Content-Type-Options','nosniff')]:handler.send_header(name,value)
        if status==206:handler.send_header('Content-Range',f'bytes {start}-{end}/{size}')
        handler.end_headers()
        if head:return
        stream.seek(start);remaining=end-start+1
        while remaining:
            chunk=stream.read(min(downloads.CHUNK,remaining))
            if not chunk:handler.close_connection=True;return
            handler.wfile.write(chunk);remaining-=len(chunk)

def bundle_selection(query):
    interval=query.get('interval','hourly');data=catalog(interval)
    start,end=query.get('start',data['days'][0]['day']),query.get('end',data['days'][-1]['day'])
    if not re.fullmatch(r'\d{4}-\d{2}-\d{2}',start) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}',end):raise ValueError('请选择有效日期')
    date.fromisoformat(start);date.fromisoformat(end)
    if not data['days'][0]['day']<=start<=end<=data['days'][-1]['day']:raise ValueError('下载日期须在项目起报范围内')
    selected=[d for d in data['days'] if start<=d['day']<=end]
    ready=[d for d in selected if d['status']=='complete']
    if not ready:raise FileNotFoundError('所选日期暂无完成的资料')
    records=[day_record(d['day'],'zip',interval,data) for d in ready]
    snapshot=hashlib.sha256(json.dumps([[d['day'],d['files']['zip']['sha256']] for d in ready]).encode()).hexdigest()
    if query.get('snapshot') and query['snapshot']!=snapshot:raise ValueError('资料目录已更新，请刷新后下载')
    manifest={k:data[k] for k in ['id','model','model_id','site','height_m','grid_description','statistic','source_collection','points',
                                 'interval','interval_minutes','temporal_method','source_manifest_sha256','license']}
    manifest.update({'start_bjt':start,'end_bjt':end,'days':[d['day'] for d in ready],
                     'missing_days':[d['day'] for d in selected if d['status']!='complete'],
                     'source_unavailable_days':[d['day'] for d in ready if d['source_status']=='unavailable'],
                     'source_partial_days':[d['day'] for d in ready if d['source_status']=='partial'],
                     'run_count':len(ready),'snapshot':snapshot})
    return manifest,records

def download_bundle(handler,query,head=False):
    if not head and not wind_projects.SLOTS.acquire(blocking=False):
        handler.reply(503,'已有两个项目下载正在传输，请稍后重试。'.encode(),'text/plain; charset=utf-8');return
    try:
        manifest,records=bundle_selection(query)
        suffix='_15min' if manifest['interval']=='15min' else ''
        handler.send_response(200)
        for name,value in [('Content-Type','application/zip'),('Cache-Control','private, no-store'),('X-Content-Type-Options','nosniff'),
                           ('Content-Disposition',f'attachment; filename="ec_balikun3_100m_{manifest["start_bjt"]}_{manifest["end_bjt"]}{suffix}.zip"'),
                           ('Connection','close')]:handler.send_header(name,value)
        handler.end_headers();handler.close_connection=True
        if head:return
        try:
            with zipfile.ZipFile(handler.wfile,'w',compression=zipfile.ZIP_STORED,allowZip64=True) as bundle:
                bundle.writestr('manifest.json',json.dumps(manifest,ensure_ascii=False,indent=2).encode())
                bundle.writestr('README.txt',(ROOT/'README.txt').read_bytes())
                for record in records:
                    with downloads.open_snapshot(record) as source,bundle.open('daily/'+record['name'],'w',force_zip64=True) as target:
                        remaining=record['bytes']
                        while remaining:
                            chunk=source.read(min(downloads.CHUNK,remaining))
                            if not chunk:raise OSError('Project file changed during transfer')
                            target.write(chunk);remaining-=len(chunk)
        except (OSError,ValueError):handler.close_connection=True
    finally:
        if not head:wind_projects.SLOTS.release()
