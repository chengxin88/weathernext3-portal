"""Read original 0.1/0.05-degree statistics from verified local chunks, offline."""
from pathlib import Path
from functools import lru_cache
from datetime import timedelta
import hashlib, json, os, tempfile
import numpy as np

REGION=(104.,17.,122.,29.)
REGION_DIRECTORY='native-south-china-v2'
REGION_SCHEMA='weathernext3-native-south-china-v2'
CHINA=(60.,0.,150.,60.)
CHINA_DIRECTORY='native-china-v1'
CHINA_SCHEMA='weathernext3-native-china-v1'
DOWNLOAD_BOUNDS=(100.,10.,130.,35.)
DOWNLOAD_DIRECTORY='native-southern-china-v1'
DOWNLOAD_SCHEMA='weathernext3-native-southern-china-v1'
SCORE_BOUNDS=(108.,18.,120.,27.)
SCORE_DIRECTORY='native-score-v1'
SCORE_SCHEMA='weathernext3-native-score-v1'
VARIABLES={
    'surface_u':('u_component_of_wind_10m_mean','m/s'),
    'surface_v':('v_component_of_wind_10m_mean','m/s'),
    'surface_t':('temperature_2m_mean','degC'),
    'surface_td':('dewpoint_temperature_2m_mean','degC'),
    'surface_msl':('mean_sea_level_pressure_mean','hPa'),
    'rain1h':('total_precipitation_1hr_mean','mm'),
    'surface_u100':('u_component_of_wind_100m_mean','m/s'),
    'surface_v100':('v_component_of_wind_100m_mean','m/s'),
    'station_t':('station_head_temperature_2m_mean','degC'),
    'station_td':('station_head_dewpoint_temperature_2m_mean','degC'),
}
CORE_KEYS=('surface_u','surface_v','surface_t','surface_td','surface_msl','rain1h')
OPTIONAL_KEYS=('imerg_rain1h','experimental_rain1h','cloud_total','cloud_high',
    'cloud_medium','cloud_low','solar_downwards','solar_direct')
VARIABLES.update({
    'imerg_rain1h':('imerg_tp_1hr_mean','mm'),
    'experimental_rain1h':('experimental_tp_1hr_mean','mm'),
    'cloud_total':('total_cloud_cover_mean','%'),
    'cloud_high':('high_cloud_cover_mean','%'),
    'cloud_medium':('medium_cloud_cover_mean','%'),
    'cloud_low':('low_cloud_cover_mean','%'),
    'solar_downwards':('surface_solar_radiation_downwards_1hr_mean','W/m2'),
    'solar_direct':('total_sky_direct_solar_radiation_at_surface_1hr_mean','W/m2'),
})
OPTIONAL_SCHEMA='weathernext3-native-optional-v1'
EXTRA_KEYS=('surface_u100','surface_v100','station_t','station_td')
FIELD_GRIDS={'station_t':.05,'station_td':.05}
EXTRA_SCHEMA='weathernext3-native-extras-v1'

def field_grid(key):return FIELD_GRIDS.get(key,.1)

def extras_path(root,run):
    from weathernext3 import utc
    return Path(root)/'native-extras-v1'/('run_'+utc(run).strftime('%Y%m%d%H')+'.json')

@lru_cache(maxsize=8)
def _extras_manifest(path,modified,size):
    return json.loads(Path(path).read_text())

def extras_manifest(root,run):
    from weathernext3 import utc
    path=extras_path(root,run)
    if not path.is_file():return None
    info=path.stat();value=_extras_manifest(str(path),info.st_mtime_ns,info.st_size)
    free_ee=value.get('source_kind')=='earth-engine-region' and value.get('earth_engine_registration')=='REGISTERED_NOT_COMMERCIALLY' and value.get('requester_pays') is False
    if value.get('schema')!=EXTRA_SCHEMA or value.get('run')!=utc(run).strftime('%Y-%m-%dT%H:%M:%SZ') or (value.get('billing_header_sent') is not False and not free_ee) or value.get('fields')!={k:field_grid(k) for k in EXTRA_KEYS}:
        raise ValueError('Native extra manifest identity mismatch')
    return value

def extra_available(root,run,lead):
    manifest=extras_manifest(root,run)
    return manifest is not None and lead in manifest.get('leads',[])

def optional_path(root,run):
    from weathernext3 import utc
    return Path(root)/'native-optional-v1'/('run_'+utc(run).strftime('%Y%m%d%H')+'.json')

def optional_manifest(root,run):
    from weathernext3 import utc
    path=optional_path(root,run)
    if not path.is_file():return None
    info=path.stat();value=_extras_manifest(str(path),info.st_mtime_ns,info.st_size)
    if (value.get('schema')!=OPTIONAL_SCHEMA or value.get('run')!=utc(run).strftime('%Y-%m-%dT%H:%M:%SZ')
            or value.get('fields')!={k:field_grid(k) for k in OPTIONAL_KEYS}):
        raise ValueError('Native optional manifest identity mismatch')
    availability=value.get('field_leads',{})
    if set(availability)!=set(OPTIONAL_KEYS):raise ValueError('Invalid optional availability')
    for leads in availability.values():
        if not isinstance(leads,list) or any(type(h)!=int or not 1<=h<=360 for h in leads) or leads!=sorted(set(leads)):
            raise ValueError('Invalid optional leads')
    return value

def optional_available(root,run,lead,key):
    manifest=optional_manifest(root,run)
    return manifest is not None and lead in manifest['field_leads'].get(key,[])

def source_coordinates(native,key):
    grid=field_grid(key);variable=VARIABLES[key][0]
    if key in EXTRA_KEYS:
        m=native.metadata[variable];a=m['attributes'];suffix='0p05' if grid==.05 else '0p1'
        shape=[1,3601,7200] if grid==.05 else [1,1801,3600]
        units='K' if key.startswith('station_') else 'm s**-1'
        if a.get('units')!=units or a.get('grid_degrees')!=grid or a.get('parameter_conversion_statistic')!='mean' or a.get('step_type')!='instant' or m.get('dimension_names')!=['lead_time','lat_'+suffix,'lon_'+suffix] or m['chunk_grid']['configuration']['chunk_shape']!=shape:
            raise ValueError('Unexpected native extra field metadata: '+variable)
    if grid==.1:return native.lon,native.lat
    if not hasattr(native,'station_lon'):
        native.station_lon=native.read('lon_0p05','c/0')
        native.station_lat=native.read('lat_0p05','c/0')
        if len(native.station_lon)!=7200 or len(native.station_lat)!=3601 or not np.allclose(native.station_lon,np.arange(7200)/20,atol=2e-5) or not np.allclose(np.sort(native.station_lat),np.arange(3601)/20-90,atol=2e-5):
            raise ValueError('Expected original global 0.05-degree coordinates')
    return native.station_lon,native.station_lat

def native_grid(source,lon,lat,bounds=None,grid_degrees=.1):
    """Reorder native coordinates and close the longitude seam; no resampling."""
    if source.shape!=(len(lat),len(lon)):raise ValueError('Native field shape mismatch')
    if grid_degrees not in (.1,.05):raise ValueError('Unsupported original grid')
    xs=np.round((np.asarray(lon,dtype=float)+180)%360-180,2)
    ys=np.round(np.asarray(lat,dtype=float),2)
    xi=np.argsort(xs);yi=np.argsort(ys)
    xs=xs[xi];ys=ys[yi]
    if not np.allclose(np.diff(xs),grid_degrees,atol=1e-8) or not np.allclose(np.diff(ys),grid_degrees,atol=1e-8):
        raise ValueError('Expected original '+str(grid_degrees)+'-degree coordinates')
    if bounds is not None:
        west,south,east,north=bounds
        xmask=(xs>=west-1e-8)&(xs<=east+1e-8);ymask=(ys>=south-1e-8)&(ys<=north+1e-8)
        xi=xi[xmask];yi=yi[ymask];xs=xs[xmask];ys=ys[ymask]
    values=np.asarray(source[np.ix_(yi,xi)],dtype=np.float32)
    if bounds is None or bounds==(-180,-90,180,90):
        xs=np.append(xs,180.);values=np.concatenate((values,values[:,:1]),axis=1)
    return xs,ys,values

def converted(key,source):
    values=np.asarray(source,dtype=np.float32).copy()
    if not np.isfinite(values).all():raise ValueError('Incomplete original field')
    if key in ('surface_t','surface_td','station_t','station_td'):values-=np.float32(273.15)
    elif key=='surface_msl':values/=np.float32(100)
    elif key in ('rain1h','imerg_rain1h','experimental_rain1h'):
        values*=np.float32(1000)
        if np.nanmin(values)<-.001 or np.nanmax(values)>1000:raise ValueError('Invalid hourly precipitation')
        np.maximum(values,0,out=values)
    elif key.startswith('cloud_'):
        if np.nanmin(values)<-1e-5 or np.nanmax(values)>1.00001:raise ValueError('Invalid cloud fraction')
        values=np.clip(values,0,1)*np.float32(100)
    elif key.startswith('solar_'):
        values/=np.float32(3600)
        if np.nanmin(values)<-.001 or np.nanmax(values)>2000:raise ValueError('Invalid hourly solar energy')
        np.maximum(values,0,out=values)
    if not np.isfinite(values).all():raise ValueError('Incomplete original field')
    return values

def china_path(root,run,lead,key,directory=CHINA_DIRECTORY):
    from weathernext3 import utc
    if key not in VARIABLES or not 1<=lead<=360:raise ValueError('Invalid China native field')
    return Path(root)/directory/utc(run).strftime('%Y%m%d%H')/f'f{lead:03}_{key}.npz'

def china_axes(key,bounds=CHINA):
    scale=round(1/field_grid(key))
    west,south,east,north=bounds
    return np.arange(round(west*scale),round(east*scale)+1)/scale,np.arange(round(south*scale),round(north*scale)+1)/scale

def save_china(root,run,lead,key,xs,ys,values,provenance,*,bounds=CHINA,directory=CHINA_DIRECTORY,schema=CHINA_SCHEMA):
    """Persist an already regional, converted native field, never resample it."""
    from weathernext3 import utc
    expected_x,expected_y=china_axes(key,bounds)
    if xs.shape!=expected_x.shape or ys.shape!=expected_y.shape or not np.allclose(xs,expected_x,atol=1e-7) or not np.allclose(ys,expected_y,atol=1e-7) or values.shape!=(len(ys),len(xs)) or values.dtype!=np.float32 or not np.isfinite(values).all():
        raise ValueError('Original China grid mismatch')
    if provenance.get('source_kind') not in ('earth-engine-region','existing-gcs-offline-cache') or not provenance.get('source_sha256'):
        raise ValueError('China source provenance absent')
    meta={**provenance,'schema':schema,'run':utc(run).strftime('%Y-%m-%dT%H:%M:%SZ'),
        'lead':lead,'valid':(utc(run)+timedelta(hours=lead)).strftime('%Y-%m-%dT%H:%M:%SZ'),
        'field':key,'unit':VARIABLES[key][1],'source_variable':VARIABLES[key][0],
        'grid_degrees':field_grid(key),'bounds':list(bounds),'statistic':'mean'}
    if key in OPTIONAL_KEYS:
        meta.update(source_unit=('m' if key.endswith('rain1h') else '(0 - 1)' if key.startswith('cloud_') else 'J m**-2'),
            source_step_type=('instant' if key.startswith('cloud_') else 'accumulated'),
            source_interval_hours=(None if key.startswith('cloud_') else 1),
            display_conversion=('multiply by 1000' if key.endswith('rain1h') else 'multiply by 100' if key.startswith('cloud_') else 'divide by 3600; 1h mean'))
    target=china_path(root,run,lead,key,directory);target.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=target.parent,delete=False) as handle:
        temporary=Path(handle.name)
        np.savez_compressed(handle,lon=xs,lat=ys,values=values,meta=np.array(json.dumps(meta)))
        handle.flush();os.fsync(handle.fileno())
    try:os.replace(temporary,target)
    finally:temporary.unlink(missing_ok=True)

def china_field(root,run,lead,key,bounds=CHINA,*,native_bounds=CHINA,directory=CHINA_DIRECTORY,schema=CHINA_SCHEMA):
    path=china_path(root,run,lead,key,directory)
    if not path.is_file():return None
    from weathernext3 import utc
    with np.load(path,allow_pickle=False) as d:
        meta=json.loads(str(d['meta']));xs,ys,values=d['lon'],d['lat'],d['values']
    expected_x,expected_y=china_axes(key,native_bounds)
    if meta.get('schema')!=schema or meta.get('run')!=utc(run).strftime('%Y-%m-%dT%H:%M:%SZ') or meta.get('lead')!=lead or meta.get('valid')!=(utc(run)+timedelta(hours=lead)).strftime('%Y-%m-%dT%H:%M:%SZ') or meta.get('field')!=key or meta.get('unit')!=VARIABLES[key][1] or meta.get('grid_degrees')!=field_grid(key) or meta.get('bounds')!=list(native_bounds) or meta.get('source_kind') not in ('earth-engine-region','existing-gcs-offline-cache') or not meta.get('source_sha256') or meta.get('statistic')!='mean':
        raise ValueError('China native identity mismatch')
    if values.shape!=(len(expected_y),len(expected_x)) or values.dtype!=np.float32 or not np.isfinite(values).all() or xs.shape!=expected_x.shape or ys.shape!=expected_y.shape or not np.allclose(xs,expected_x,atol=1e-7) or not np.allclose(ys,expected_y,atol=1e-7):
        raise ValueError('China native coordinates mismatch')
    west,south,east,north=bounds
    xi=np.flatnonzero((xs>=west-1e-8)&(xs<=east+1e-8));yi=np.flatnonzero((ys>=south-1e-8)&(ys<=north+1e-8))
    return xs[xi],ys[yi],values[np.ix_(yi,xi)]

def download_axes(key):return china_axes(key,DOWNLOAD_BOUNDS)

def download_path(root,run,lead,key):return china_path(root,run,lead,key,DOWNLOAD_DIRECTORY)

def save_download(root,run,lead,key,xs,ys,values,provenance):
    return save_china(root,run,lead,key,xs,ys,values,provenance,
        bounds=DOWNLOAD_BOUNDS,directory=DOWNLOAD_DIRECTORY,schema=DOWNLOAD_SCHEMA)

def download_field(root,run,lead,key,bounds=DOWNLOAD_BOUNDS):
    return china_field(root,run,lead,key,bounds,native_bounds=DOWNLOAD_BOUNDS,
        directory=DOWNLOAD_DIRECTORY,schema=DOWNLOAD_SCHEMA)


def region_path(root,run,lead,key):
    if key not in VARIABLES:raise ValueError('Unsupported native surface field')
    from weathernext3 import utc
    return Path(root)/REGION_DIRECTORY/utc(run).strftime('%Y%m%d%H')/f'f{lead:03}_{key}.npz'

def save_region(root,run,lead,key,source,lon,lat,provenance):
    from weathernext3 import utc
    target=region_path(root,run,lead,key)
    if target.is_file():return
    grid=field_grid(key)
    xs,ys,values=native_grid(source,lon,lat,REGION,grid);values=converted(key,values)
    meta={'schema':REGION_SCHEMA,'run':utc(run).strftime('%Y-%m-%dT%H:%M:%SZ'),'lead':lead,
        'valid':(utc(run)+timedelta(hours=lead)).strftime('%Y-%m-%dT%H:%M:%SZ'),
        'grid_degrees':grid,'bounds':list(REGION),'field':key,'unit':VARIABLES[key][1],
        'source_variable':VARIABLES[key][0],'source_generation':provenance.get('generation'),
        'source_sha256':provenance.get('sha256'),'billing_header_sent':False}
    if not meta['source_generation'] or not meta['source_sha256']:raise ValueError('Native source provenance missing')
    target.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=target.parent,delete=False) as handle:
        temporary=Path(handle.name)
        np.savez(handle,lon=xs,lat=ys,values=values,meta=np.array(json.dumps(meta)))
    try:os.replace(temporary,target)
    finally:temporary.unlink(missing_ok=True)

def region_field(root,run,lead,key,bounds):
    path=region_path(root,run,lead,key)
    if not path.is_file():return None
    from weathernext3 import utc
    with np.load(path,allow_pickle=False) as d:
        meta=json.loads(str(d['meta']))
        if meta.get('schema')!=REGION_SCHEMA or meta.get('run')!=utc(run).strftime('%Y-%m-%dT%H:%M:%SZ') or meta.get('lead')!=lead or meta.get('field')!=key or meta.get('grid_degrees')!=field_grid(key) or meta.get('bounds')!=list(REGION) or meta.get('billing_header_sent') is not False:
            raise ValueError('Native cache identity mismatch')
        xs,ys,values=d['lon'],d['lat'],d['values']
    scale=round(1/field_grid(key))
    expected_x=np.arange(round(REGION[0]*scale),round(REGION[2]*scale)+1)/scale
    expected_y=np.arange(round(REGION[1]*scale),round(REGION[3]*scale)+1)/scale
    if values.shape!=(len(expected_y),len(expected_x)) or values.dtype!=np.float32 or not np.isfinite(values).all() or xs.shape!=expected_x.shape or ys.shape!=expected_y.shape or not np.allclose(xs,expected_x) or not np.allclose(ys,expected_y):
        raise ValueError('Native regional grid mismatch')
    west,south,east,north=bounds
    xi=np.flatnonzero((xs>=west-1e-8)&(xs<=east+1e-8));yi=np.flatnonzero((ys>=south-1e-8)&(ys<=north+1e-8))
    return xs[xi],ys[yi],values[np.ix_(yi,xi)]

class OfflineClient:
    def __init__(self,root,run,prefix):
        from weathernext3 import utc,BUCKET,VERSION
        self.run=utc(run);self.prefix=prefix
        if not prefix.startswith(VERSION+'/zarr/2026_to_present/'+self.run.strftime('%Y%m%d_%Hhr_')) or not prefix.endswith('/predictions.zarr/'):
            raise ValueError('Native source prefix mismatch')
        self.directory=Path(root)/'raw'/self.run.strftime('%Y%m%d%H')/'predictions.zarr'
        self.objects={}
    def select_run(self,run):
        from weathernext3 import utc
        if utc(run)!=self.run:raise ValueError('Native source run mismatch')
        return self.prefix
    def media(self,name):
        from weathernext3 import WeatherNext3Error
        if not name.startswith(self.prefix):raise ValueError('Native source escaped its run')
        relative=name[len(self.prefix):]
        if not relative or any(p in ('','..') for p in relative.split('/')):raise ValueError('Invalid native source path')
        path=self.directory/relative;record=path.with_name(path.name+'.source.json')
        if not path.is_file() or not record.is_file():
            raise WeatherNext3Error('unavailable','本起报的原始缓存不可用；请选最近三天的完整起报。',http_status=404)
        meta=json.loads(record.read_text());content=path.read_bytes()
        if not meta.get('generation') or len(content)!=meta.get('bytes') or hashlib.sha256(content).hexdigest()!=meta.get('sha256'):
            raise ValueError('Native source checksum mismatch')
        self.objects[name]=meta
        return content

@lru_cache(maxsize=4)
def offline_run(root,run,prefix):
    from prepare_weathernext3 import NativeRun
    return NativeRun(OfflineClient(root,run,prefix),run)

def read_field(root,c,lead,key,bounds=None):
    from weathernext3 import utc,BUCKET
    root=Path(root);run=utc(c['run'])
    bounds=tuple(c.get('bounds',DOWNLOAD_BOUNDS)) if bounds is None else tuple(bounds)
    if not DOWNLOAD_BOUNDS[0]<=bounds[0]<bounds[2]<=DOWNLOAD_BOUNDS[2] or not DOWNLOAD_BOUNDS[1]<=bounds[1]<bounds[3]<=DOWNLOAD_BOUNDS[3]:
        raise ValueError('WeatherNext 3当前下载范围为100–130°E、10–35°N')
    if lead<1 or lead>max(f['lead'] for f in c['frames']):return None
    if key in ('surface_rh','station_rh'):
        prefix='station_' if key=='station_rh' else 'surface_'
        first=read_field(root,c,lead,prefix+'t',bounds)
        second=read_field(root,c,lead,prefix+'td',bounds)
        if first is None or second is None:return None
        from prepare_weathernext3 import relative_humidity
        return first[0],first[1],relative_humidity(first[2],second[2])
    if key not in VARIABLES:raise ValueError('Unsupported native surface field')
    if key in EXTRA_KEYS and not extra_available(root,run,lead):return None
    if key in OPTIONAL_KEYS and not optional_available(root,run,lead,key):return None
    if c.get('native_cache_directory')==SCORE_DIRECTORY:
        if not SCORE_BOUNDS[0]<=bounds[0]<bounds[2]<=SCORE_BOUNDS[2] or not SCORE_BOUNDS[1]<=bounds[1]<bounds[3]<=SCORE_BOUNDS[3]:
            raise ValueError('WeatherNext 3 超过7天的资料仅保留108–120°E、18–27°N评分区域')
        return china_field(root,run,lead,key,bounds,native_bounds=SCORE_BOUNDS,directory=SCORE_DIRECTORY,schema=SCORE_SCHEMA)
    result=download_field(root,run,lead,key,bounds)
    if result is not None:return result
    result=china_field(root,run,lead,key,bounds)
    if result is not None:return result
    if c.get('source_kind')=='earth-engine-region':return None
    regional=bounds is not None and REGION[0]<=bounds[0]<=bounds[2]<=REGION[2] and REGION[1]<=bounds[1]<=bounds[3]<=REGION[3]
    if regional:
        result=region_field(root,run,lead,key,bounds)
        if result is not None:return result
    frame=root/c['frames'][0]['file'];identity=json.loads(frame.read_text())
    if identity.get('run')!=c['run'] or identity.get('billing_header_sent') is not False:raise ValueError('Native frame identity mismatch')
    source_path=identity['source_path'];bucket='gs://'+BUCKET+'/'
    if not source_path.startswith(bucket):raise ValueError('Unexpected native source bucket')
    native=offline_run(str(root),c['run'],source_path[len(bucket):])
    variable=VARIABLES[key][0];source=native.source_field(variable,lead)
    lon,lat=source_coordinates(native,key)
    name=native.prefix+variable+f'/c/{lead-1}/0/0'
    save_region(root,run,lead,key,source,lon,lat,native.client.objects[name])
    if regional:return region_field(root,run,lead,key,bounds)
    xs,ys,values=native_grid(source,lon,lat,CHINA,field_grid(key))
    provenance=native.client.objects[name]
    save_china(root,run,lead,key,xs,ys,converted(key,values),{
        'source_kind':'existing-gcs-offline-cache','source_sha256':provenance['sha256'],
        'source_generation':provenance['generation'],'source_path':source_path+variable+f'/c/{lead-1}/0/0',
        'billing_header_sent':False})
    return china_field(root,run,lead,key,bounds)

def bilinear(xs,ys,field,lon,lat):
    if not xs[0]<=lon<=xs[-1] or not ys[0]<=lat<=ys[-1]:raise ValueError('Station outside original grid')
    i=int(np.clip(np.searchsorted(xs,lon,side='right')-1,0,len(xs)-2))
    j=int(np.clip(np.searchsorted(ys,lat,side='right')-1,0,len(ys)-2))
    x=(lon-xs[i])/(xs[i+1]-xs[i]);y=(lat-ys[j])/(ys[j+1]-ys[j])
    return float(field[j,i]*(1-x)*(1-y)+field[j,i+1]*x*(1-y)+field[j+1,i]*(1-x)*y+field[j+1,i+1]*x*y)
