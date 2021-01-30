#!/usr/bin/env python3

from collections import deque
from pathlib import Path

import sys
import os
import argparse
import shlex
import datetime
import subprocess
import itertools
import math

# Remove line from top of lines and calculate indentation and return
def pop_line(lines):
  line = lines.popleft()
  c = line.lstrip()
  indent = len(line) - len(c)
  return (line, c, indent)

# Parse indentation tree
# 
# input:
# FILE "x.wav" WAVE
#   TRACK 01 AUDIO
#   TRACK 02 AUDIO
# FILE "y.wav" WAVE
#   TRACK 03 AUDIO
# 
# output:
# {
#   'FILE': [
#     { '': ['x.wav', 'WAVE'],
#       'TRACK': [
#         { '': ['01', 'AUDIO'] },
#         { '': ['02', 'AUDIO'] },
#       ],
#     },
#     { '': ['y.wav', 'WAVE'],
#       'TRACK': [
#         { '': ['03', 'AUDIO'] },
#       ]
#     },
#   ]
# }
def simple_parse(lines, depth = 0):
  out = {}
  while len(lines) > 0:
    (line, l, indent) = pop_line(lines)
    (key, _, value) = l.partition(' ')
    if not key in out:
      out[key] = []
    obj = { '': shlex.split(value) }
    out[key].append(obj)
    try:
      (next_line, _, next_indent) = pop_line(lines)
    except IndexError:
      break
    lines.appendleft(next_line)
    if next_indent > indent:
      obj.update(simple_parse(lines, depth + 1))
    elif next_indent < indent:
      break
  return out

# Parse mm:ss:ff (ff are 75 fps frames)
def parse_time(time):
  [minutes, seconds, frames] = map(int, time.split(':'))
  return datetime.timedelta(
    minutes=minutes,
    seconds=seconds,
    milliseconds=(frames / 75) * 1000
  )

def main(argv=[]):
  parser = argparse.ArgumentParser()
  parser.add_argument('cue_file', help='Path to cue file to split')
  parser.add_argument('-E', '--cue-encoding', default='UTF8', help='The text encoding of the CUE file')
  parser.add_argument('-n', '--dry-run', help='Print ffmpeg commands', action='store_true')
  parser.add_argument('-o', '--output-path', type=Path, default='.', help='Path to output to')
  parser.add_argument('-e', '--output-encoding', default='flac', help='Output file encoding')
  args = parser.parse_args(argv)
  
  cue_file = Path(args.cue_file).resolve()
  cue_dir = cue_file.parent
  
  cue_data = deque([l.rstrip() for l in open(cue_file, encoding=args.cue_encoding)])
  cue = simple_parse(cue_data)
  
  metadata = {}
  tracks = []
  file = { 'metadata': metadata, 'tracks': tracks }
  
  # TODO: Handle multifile cues?
  file['track_count'] = len(cue['FILE'][0]['TRACK'])
  
  file['path'] = cue_dir / cue['FILE'][0][''][0]
  metadata['album'] = cue['TITLE'][0][''][0]
  if 'PERFORMER' in cue:
    metadata['album_artist'] = cue['PERFORMER'][0][''][0]
  if 'SONGWRITER' in cue:
    metadata['composer'] = cue['SONGWRITER'][0][''][0]
  
  if 'REM' in cue:
    for r in cue['REM']:
      [field, *rem_args] = r['']
      if field == 'GENRE':
        metadata['genre'] = rem_args[0]
      elif field == 'DATE':
        metadata['DATE'] = rem_args[0]
      elif field == 'DISKID':
        pass
      elif field == 'COMMENT':
        pass
      elif field == 'REPLAYGAIN_ALBUM_GAIN':
        metadata['replaygain_album_gain'] = ' '.join(rem_args)
      elif field == 'REPLAYGAIN_ALBUM_PEAK':
        metadata['replaygain_album_peak'] = ' '.join(rem_args)
  
  
  for f in cue['FILE']:
    for t in f['TRACK']:
      metadata = {}
      track = { 'metadata': metadata }
      track['id'] = int(t[''][0])
      metadata['track'] = f"{track['id']}/{file['track_count']}"
      metadata['title'] = t['TITLE'][0][''][0]
      if 'PERFORMER' in t:
        metadata['artist'] = t['PERFORMER'][0][''][0]
      if 'SONGWRITER' in t:
        metadata['composer'] = t['SONGWRITER'][0][''][0]
      if 'REPLAYGAIN_TRACK_GAIN' in t:
        metadata['replaygain_track_gain'] = t['REPLAYGAIN_TRACK_GAIN'][0][''][0]
      if 'REPLAYGAIN_TRACK_PEAK' in t:
        metadata['replaygain_track_peak'] = t['REPLAYGAIN_TRACK_PEAK'][0][''][0]
      
      if 'REM' in t:
        for r in t['REM']:
          [field, *rem_args] = r['']
          if field == 'REPLAYGAIN_TRACK_GAIN':
            metadata['replaygain_track_gain'] = ' '.join(rem_args)
          elif field == 'REPLAYGAIN_TRACK_PEAK':
            metadata['replaygain_track_peak'] = ' '.join(rem_args)
      
      for index in t['INDEX']:
        if int(index[''][0]) == int('01'):
          track['start_time'] = parse_time(index[''][1])
        elif int(index[''][0]) == int('00'):
          track['pregap_time'] = parse_time(index[''][1])
      
      tracks.append(track)
  
  # Order by start time
  time_ordered_tracks = deque(reversed(sorted(tracks,
    key = lambda t: t['start_time'])))
  
  # Calculate track end times by previous track pregap time
  end_time = None
  for track in time_ordered_tracks:
    start_time = track.get('start_time')
    pregap_time = track.get('pregap_time', start_time)
    track['end_time'] = end_time
    if end_time is not None and start_time is not None:
      track['duration'] = end_time - start_time
    end_time = pregap_time
  
  ffmpeg = ['ffmpeg']
  
  file_meta_args = [f'{k.upper()}={v}' for (k, v) in file['metadata'].items()]
  track_padding = math.ceil(math.log10(len(file['tracks'])))
  
  for track in file['tracks']:
    track_meta_args = [f'{k.upper()}={v}' for (k, v) in track['metadata'].items()]
    meta_args = list(itertools.chain.from_iterable([['-metadata', v] for v in file_meta_args + track_meta_args]))
    try:
      file_author = track['metadata']['author']
    except KeyError:
      file_author = file['metadata']['album_artist']
    track_title = track['metadata']['title']
    encoding = args.output_encoding
    out_filename = f"{track['id']:0{track_padding}d} - {file_author} - {track['metadata']['title']}.{encoding}"
    out_filename = out_filename.replace(os.sep, '')
    
    command = (ffmpeg
      + [
        '-ss', str(track['start_time']),
        '-i', str(file['path']),
        # TODO: argparse codec default='flac'
        #'-c:a', encoding,
        '-vn', # ffmpeg interprets album art as video
      ]
      + (['-t', str(track['duration'])] if 'duration' in track else [])
      + meta_args
      + [ str(args.output_path.resolve() /
        out_filename) ])
    
    print(' '.join(command))
    if not args.dry_run:
      subprocess.run(command)

if __name__ == '__main__':
  main(sys.argv[1:])
