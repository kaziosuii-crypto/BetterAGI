// 演示素材生成：背景视频（ffmpeg）+ TTS 语音（SAPI）
import fsp from 'node:fs/promises';
import path from 'node:path';
import { spawn } from 'node:child_process';
import { ProjectStore } from '../server/store.mjs';
import { generateProjectTTS } from '../server/tts.mjs';

function run(cmd, args, opts) {
  return new Promise(function (resolve, reject) {
    var child = spawn(cmd, args, { stdio: ['ignore', 'pipe', 'pipe'], windowsHide: true });
    var err = '';
    child.stdout.on('data', function () {});
    child.stderr.on('data', function (chunk) { err += chunk; });
    child.on('error', reject);
    child.on('close', function (code) {
      if (code === 0) resolve(err);
      else reject(new Error(cmd + ' exited ' + code + ': ' + err.slice(-800)));
    });
  });
}

function have(cmd) {
  return new Promise(function (resolve) {
    var child = spawn(cmd, ['-version'], { stdio: ['ignore', 'pipe', 'ignore'], windowsHide: true });
    child.on('error', function () { resolve(false); });
    child.on('close', function (code) { resolve(code === 0); });
  });
}

export async function buildVideo(outFile, seconds) {
  var tmp = path.join(path.dirname(outFile), '_backdrop.png');
  await fsp.mkdir(path.dirname(outFile), { recursive: true });
  await run('ffmpeg', ['-hide_banner', '-y',
    '-f', 'lavfi',
    '-i', 'gradients=s=1920x1080:c0=0x070910:c1=0x141d2c:c2=0x3d2a13:nb_colors=3:x0=240:y0=980:x1=1700:y1=120:d=1',
    '-frames:v', '1', tmp]);
  var dur = String(Math.max(6, Math.round(seconds)));
  var vf = [
    "zoompan=z='min(1.10,1+0.0035*on)':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d=1:s=1920x1080:fps=30",
    'drawgrid=w=160:h=160:t=1:c=0xFFFFFF@0.030',
    'drawbox=x=0:y=0:w=iw:h=180:color=0x000000@0.18:t=fill',
    'drawbox=x=0:y=ih-220:w=iw:h=220:color=0x000000@0.26:t=fill',
    "drawbox=x=120:y='mod(t*40,1080)':w=iw-240:h=1:color=0xE8B05A@0.30:t=fill",
    "drawbox=x='mod(t*24,1920)':y=0:w=1:h=ih:color=0x7FA8D9@0.16:t=fill",
    'noise=alls=3:allf=t',
    'vignette=PI/4.6',
    'format=yuv420p'
  ].join(',');
  await run('ffmpeg', ['-hide_banner', '-y', '-loop', '1', '-framerate', '30', '-i', tmp, '-t', dur,
    '-vf', vf, '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '23', '-movflags', '+faststart', outFile]);
  await fsp.rm(tmp, { force: true });
  return outFile;
}

export async function ensureDemo(opts) {
  opts = opts || {};
  var root = opts.root || path.resolve('.');
  var store = new ProjectStore(root);
  await store.init();
  var project = await store.load('demo');
  var mediaDir = path.join(store.projectDir('demo'), 'media');
  await fsp.mkdir(mediaDir, { recursive: true });
  var videoFile = path.join(mediaDir, 'demo.mp4');
  var videoBuilt = false;
  if (opts.force || !(await fsp.stat(videoFile).catch(function () { return null; }))) {
    if (!(await have('ffmpeg'))) throw new Error('需要 ffmpeg 才能生成演示背景视频，请安装并加入 PATH');
    await buildVideo(videoFile, 30);
    videoBuilt = true;
  }
  var tts = await generateProjectTTS(store, 'demo', {
    force: !!opts.force,
    onProgress: function (p) {
      if (!opts.quiet && p.phase === 'cue') process.stdout.write('\r  TTS ' + p.index + '/' + p.total + '  ');
    }
  });
  if (!opts.quiet) process.stdout.write('\n');
  return { project: project, video: videoFile, videoBuilt: videoBuilt, tts: tts };
}
