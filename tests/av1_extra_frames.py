#!/usr/bin/env python3
"""Generate real multi-tile/lossy streams for pinned header oracle tests."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess


def main():
    p=argparse.ArgumentParser();p.add_argument('--out',type=Path,required=True);a=p.parse_args()
    a.out.mkdir(parents=True,exist_ok=True)
    cases=[]
    width,height=256,128
    luma=bytes((x*17+y*13)%256 for y in range(height) for x in range(width))
    chroma=bytes((x*7+y*11)%256 for y in range(height//2) for x in range(width//2))
    version=subprocess.run(['ffmpeg','-version'],check=True,capture_output=True,text=True).stdout.splitlines()[0]
    for name,fmt,quality,data in [('multi-lossless','gray',0,luma),('multi-lossy','gray',35,luma),
        ('multi-color','yuv420p',35,luma+chroma+chroma[::-1])]:
        raw=a.out/(name+'.raw');raw.write_bytes(data);obu=a.out/(name+'.obu')
        command=['ffmpeg','-hide_banner','-loglevel','error','-f','rawvideo','-pixel_format',fmt,
            '-video_size',f'{width}x{height}','-framerate','1','-i',str(raw),'-frames:v','1',
            '-c:v','libaom-av1','-threads','1','-row-mt','0','-cpu-used','8','-crf',str(quality),
            '-b:v','0','-still-picture','1','-tile-columns','2','-tile-rows','1','-f','obu','-y',str(obu)]
        subprocess.run(command,check=True)
        cases.append(dict(name=name,path=str(obu),sha256=hashlib.sha256(obu.read_bytes()).hexdigest(),command=command))
    (a.out/'fixtures.json').write_text(json.dumps(dict(encoder=version,cases=cases),indent=2)+'\n')
    print('Generated 3 real multi-tile/lossy header oracle fixtures')


if __name__=='__main__':main()
