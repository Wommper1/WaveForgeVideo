from __future__ import annotations
import array, base64, collections, json, os, queue, shutil, signal, subprocess, sys, threading, time
from dataclasses import dataclass
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

__version__ = "1.1.0"

APP_DIR = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
# Settings and logs live in the user's profile, so the app also works from a read-only folder.
# Set WAVEFORGE_HOME to keep them somewhere else (for example next to a portable copy).
DATA_DIR = Path(os.environ.get("WAVEFORGE_HOME") or Path(os.environ.get("APPDATA") or Path.home() / ".config") / "WaveForgeVideo")
CONFIG = DATA_DIR / "config.json"
LOG_DIR = DATA_DIR / "logs"

VIDEO_EXTS = {
    ".mp4",".mkv",".mov",".avi",".webm",".m4v",".mpeg",".mpg",".ts",".mts",".m2ts",
    ".wmv",".flv",".3gp",".ogv",".vob",".mxf",".rm",".rmvb"
}
AUDIO_EXTS = {".mp3",".wav",".flac",".m4a",".aac",".ogg",".opus",".wma",".ac3"}
MEDIA_FILETYPES = [("Video and audio"," ".join("*"+e for e in sorted(VIDEO_EXTS|AUDIO_EXTS))),("All files","*.*")]

# Keeps ffmpeg/ffprobe from flashing a console window when the app runs without one.
_NO_WINDOW = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
# Outside Windows an export runs in its own process group so cancelling can stop all of it.
_OWN_GROUP = {} if os.name == "nt" else {"start_new_session": True}

ROTATE_FILTERS = {"90° clockwise": "transpose=1", "90° counterclockwise": "transpose=2", "180°": "hflip,vflip"}

@dataclass
class MediaInfo:
    path: str
    duration: float = 0.0
    width: int = 0
    height: int = 0
    fps: float = 0.0
    video_codec: str = ""
    audio_codec: str = ""
    container: str = ""
    size_bytes: int = 0

def default_output_dir():
    videos = Path.home() / "Videos"
    return str(videos if videos.is_dir() else Path.home())

def load_config():
    default = {
        "ffmpeg_path": "",
        "ffprobe_path": "",
        "prefer_hardware": True,
        "last_output_dir": default_output_dir()
    }
    if CONFIG.exists():
        try:
            default.update(json.loads(CONFIG.read_text(encoding="utf-8")))
        except Exception:
            pass
    return default

def save_config(cfg):
    try:
        CONFIG.parent.mkdir(parents=True, exist_ok=True)
        CONFIG.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    except OSError:
        pass  # settings just won't persist

def extra_search_dirs():
    """Places FFmpeg usually ends up on Windows without being on PATH."""
    dirs=[]
    local=os.environ.get("LOCALAPPDATA")
    if local:
        winget=Path(local,"Microsoft","WinGet","Packages")  # winget install Gyan.FFmpeg
        dirs += sorted(winget.glob("*FFmpeg*/*/bin"),reverse=True) + sorted(winget.glob("*FFmpeg*/bin"),reverse=True)
    for root in (os.environ.get("ProgramFiles"), os.environ.get("SystemDrive","C:")+"\\"):
        if root:dirs += [Path(root,"ffmpeg","bin"), Path(root,"ffmpeg")]
    return dirs

def find_executable(name, configured=""):
    """Return the path of an existing ffmpeg/ffprobe file, or "" if none is found.

    `configured` may be the executable itself or the folder that contains it.
    """
    candidates=[]
    if configured:
        c=Path(configured)
        candidates.append(c/name if c.is_dir() else c)
    candidates += [APP_DIR/name, APP_DIR/"bin"/name]
    for found in (shutil.which(name), shutil.which(name + ".exe")):
        if found:
            candidates.append(Path(found))
    for c in candidates:
        if c.is_file():
            return str(c)
    for d in extra_search_dirs():
        if (d/name).is_file():
            return str(d/name)
    return ""

def run_capture(cmd, timeout=30):
    p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout, **_NO_WINDOW)
    if p.returncode != 0:
        raise RuntimeError((p.stderr or p.stdout or "Command failed")[-2000:])
    return p.stdout

def parse_fraction(v):
    try:
        if "/" in v:
            a,b=v.split("/",1)
            return float(a)/float(b) if float(b) else 0.0
        return float(v)
    except Exception:
        return 0.0

def probe_media(ffprobe, path):
    out = run_capture([
        ffprobe, "-v","error","-print_format","json","-show_format","-show_streams", path
    ])
    data = json.loads(out)
    info=MediaInfo(path=path, size_bytes=Path(path).stat().st_size)
    fmt=data.get("format",{})
    info.container=fmt.get("format_long_name") or fmt.get("format_name","")
    try: info.duration=float(fmt.get("duration") or 0)
    except (TypeError, ValueError): pass
    for s in data.get("streams",[]):
        if s.get("codec_type")=="video" and not info.video_codec:
            info.video_codec=s.get("codec_name","")
            info.width=int(s.get("width") or 0); info.height=int(s.get("height") or 0)
            info.fps=parse_fraction(s.get("avg_frame_rate","0"))
        elif s.get("codec_type")=="audio" and not info.audio_codec:
            info.audio_codec=s.get("codec_name","")
    return info

def peaks_from_pcm(data, buckets):
    """Peak level (0..1) of each of `buckets` equal slices of signed 16-bit mono PCM."""
    samples=array.array("h")
    samples.frombytes(data[:len(data)//2*2])
    if sys.byteorder=="big":samples.byteswap()
    n=len(samples)
    if not n:return []
    buckets=max(1,min(buckets,n))
    peaks=[]
    for i in range(buckets):
        chunk=samples[i*n//buckets:(i+1)*n//buckets]
        peaks.append(max(max(chunk),-min(chunk))/32768)
    return peaks

def compute_waveform(ffmpeg, path, buckets=1000, timeout=300):
    """Waveform peaks of the first audio track, decoded by ffmpeg; [] if there is none or decoding fails."""
    cmd=[ffmpeg,"-v","error","-i",path,"-map","0:a:0","-ac","1","-ar","2000","-f","s16le","-"]
    try:
        p=subprocess.run(cmd,capture_output=True,timeout=timeout,**_NO_WINDOW)
    except (OSError,subprocess.TimeoutExpired):
        return []
    if p.returncode!=0:return []
    return peaks_from_pcm(p.stdout,buckets)

def fmt_time(t):
    t=int(max(0,t));h,rem=divmod(t,3600);m,s=divmod(rem,60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"

def preview_time(t, duration):
    """Clamp `t` to a moment that has a frame; the very end of a file does not."""
    t=max(0.0,t)
    return min(t,max(0.0,duration-0.05)) if duration>0 else t

def grab_frame(ffmpeg, path, t, rotate="None", box=(320,180), timeout=30):
    """PNG bytes of the video frame at `t` seconds, shrunk to fit `box` and rotated like the export; b"" if none."""
    vf=([ROTATE_FILTERS[rotate]] if rotate in ROTATE_FILTERS else [])+[f"scale={box[0]}:{box[1]}:force_original_aspect_ratio=decrease"]
    cmd=[ffmpeg,"-v","error","-ss",f"{max(0.0,t):.3f}","-i",path,"-map","0:v:0","-frames:v","1","-an","-sn",
         "-vf",",".join(vf),"-pix_fmt","rgb24","-f","image2pipe","-c:v","png","-"]
    try:
        p=subprocess.run(cmd,capture_output=True,timeout=timeout,**_NO_WINDOW)
    except (OSError,subprocess.TimeoutExpired):
        return b""
    return p.stdout if p.returncode==0 and p.stdout.startswith(b"\x89PNG") else b""

class FrameLoader:
    """Fetches preview frames one at a time on a background thread.

    Each slot ("start", "end", "hover") keeps only its newest request, so dragging the mouse never
    builds up a backlog of ffmpeg runs, and frames already fetched come from a small cache.
    `grab(*key)` returns PNG bytes; `deliver(slot, ticket, key, data)` is called from either thread.
    """
    def __init__(self, grab, deliver, cache_size=64):
        self._grab=grab;self._deliver=deliver;self._cache_size=cache_size
        self._cache=collections.OrderedDict()
        self._latest={};self._count=0
        self._lock=threading.Lock()
        self._queue=queue.Queue()
        threading.Thread(target=self._loop,daemon=True).start()

    def request(self, slot, key):
        """Ask for the frame described by `key` (a tuple of grab arguments); returns the request's ticket."""
        with self._lock:
            self._count+=1;n=self._count;self._latest[slot]=n
            data=self._cache.get(key)
            if data is not None:self._cache.move_to_end(key)
        if data is not None:self._deliver(slot,n,key,data)
        else:self._queue.put((slot,n,key))
        return n

    def cancel(self, slot):
        """Make any request still pending or running for `slot` stale."""
        with self._lock:
            self._count+=1;self._latest[slot]=self._count

    def current(self, slot, ticket):
        return self._latest.get(slot)==ticket

    def clear_cache(self):
        with self._lock:self._cache.clear()

    def _loop(self):
        while True:
            slot,n,key=self._queue.get()
            if not self.current(slot,n):continue
            try:data=self._grab(*key)
            except Exception:data=b""  # one bad frame must not stop the loader
            if data:
                with self._lock:
                    self._cache[key]=data
                    while len(self._cache)>self._cache_size:self._cache.popitem(last=False)
            self._deliver(slot,n,key,data)

def detect_encoders(ffmpeg):
    try:
        txt=run_capture([ffmpeg,"-hide_banner","-encoders"],timeout=15)
    except Exception:
        return set()
    found=set()
    for name in ["h264_nvenc","hevc_nvenc","h264_qsv","hevc_qsv","h264_amf","hevc_amf","libx264","libx265","libvpx-vp9"]:
        if name in txt: found.add(name)
    return found

def choose_encoder(encoders, codec, prefer_hw):
    if codec=="h264":
        if prefer_hw:
            for e in ("h264_nvenc","h264_qsv","h264_amf"):
                if e in encoders:return e
        return "libx264"
    if codec=="h265":
        if prefer_hw:
            for e in ("hevc_nvenc","hevc_qsv","hevc_amf"):
                if e in encoders:return e
        return "libx265"
    if codec=="vp9":
        return "libvpx-vp9"
    return "libx264"

def is_hw_encoder(name):
    return name.endswith(("_nvenc","_qsv","_amf"))

def verify_encoder(ffmpeg, encoder, timeout=15):
    """True if `encoder` can really encode a frame on this PC.

    `ffmpeg -encoders` lists NVENC/QSV/AMF in typical Windows builds whether or not
    the matching GPU exists, so listing alone says nothing about usability.
    """
    cmd=[ffmpeg,"-hide_banner","-loglevel","error","-f","lavfi","-i","color=c=black:s=256x256:r=1:d=1",
         "-frames:v","1","-an","-c:v",encoder,"-f","null","-"]
    try:
        run_capture(cmd,timeout=timeout)
    except Exception:
        return False
    return True

def same_file(a, b):
    try:
        return os.path.samefile(a,b)
    except OSError:
        return os.path.normcase(os.path.abspath(a))==os.path.normcase(os.path.abspath(b))

def kill_process_tree(proc):
    """Stop `proc` and everything it started.

    FFmpeg installed through a launcher shim (Chocolatey, Scoop) is a small exe whose real
    ffmpeg.exe runs as a child, so terminating only the launcher would leave the encode going.
    """
    if proc.poll() is not None:return
    try:
        if os.name=="nt":
            subprocess.run(["taskkill","/F","/T","/PID",str(proc.pid)],capture_output=True,**_NO_WINDOW)
        else:
            os.killpg(proc.pid,signal.SIGTERM)  # the process was started in its own group
    except OSError:pass
    if proc.poll() is None:
        try:proc.terminate()
        except OSError:pass

def partial_path(output):
    """Temporary name used while exporting, so an existing file is only replaced by a finished one."""
    p=Path(output)
    return str(p.with_name(p.stem+".part"+p.suffix))

class Job:
    def __init__(self, cmd, output, label, total=0.0, fallback_cmd=None, work_path=None):
        self.cmd=cmd; self.output=output; self.label=label
        self.total=total  # seconds of output, for the progress bar
        self.fallback_cmd=fallback_cmd  # software-encoder retry if a hardware encoder fails
        self.work_path=work_path or output  # ffmpeg writes here; renamed to `output` once it succeeds
        self.proc=None; self.cancelled=False

    def cancel(self):
        self.cancelled=True
        if self.proc:kill_process_tree(self.proc)

    def discard_partial(self):
        if self.work_path!=self.output:
            try:Path(self.work_path).unlink()
            except OSError:pass

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("WAVEFORGE VIDEO")
        self.geometry("1220x780")
        self.minsize(1050,650)
        self.configure(bg="#0a0e14")
        self.protocol("WM_DELETE_WINDOW",self._on_close)

        self.cfg=load_config()
        self.ffmpeg=find_executable("ffmpeg.exe",self.cfg.get("ffmpeg_path","")) or find_executable("ffmpeg",self.cfg.get("ffmpeg_path",""))
        self.ffprobe=find_executable("ffprobe.exe",self.cfg.get("ffprobe_path","")) or find_executable("ffprobe",self.cfg.get("ffprobe_path",""))
        self.encoders=set()
        self._enc_gen=0  # bumps on each hardware re-check so stale results are ignored
        self.media:MediaInfo|None=None
        self.wave:list|None=None  # None while the waveform is still being computed
        self.current_job:Job|None=None
        self.events=queue.Queue()
        self.frames=FrameLoader(lambda path,t,rot,w,h:grab_frame(self.ffmpeg,path,t,rot,(w,h)),
                                lambda slot,n,key,data:self.events.put(("frame",slot,n,key,data)))
        self.shown={}  # slot -> key of the frame on screen
        self.preview_box=(300,169)  # size frames are fetched at for the START / END panels
        self._frame_content={}  # slot -> ("img", PhotoImage) or ("text", message); also keeps images alive
        self._hover_x=None;self._hover_img=None
        self._preview_after=None;self._hover_after=None

        self._style()
        self._build()
        self.after(100,self._poll)
        self._boot_checks()

    def _style(self):
        s=ttk.Style(self)
        try:s.theme_use("clam")
        except tk.TclError:pass
        s.configure(".", background="#0f1621", foreground="#eef4ff", fieldbackground="#0b1119", bordercolor="#28364d")
        s.configure("TFrame", background="#0f1621")
        s.configure("TLabelframe", background="#0f1621", foreground="#c9d5e7")
        s.configure("TLabelframe.Label", background="#0f1621", foreground="#7ee7ff", font=("Segoe UI",10,"bold"))
        s.configure("TLabel", background="#0f1621", foreground="#dce7f7")
        s.configure("TButton", background="#1b2638", foreground="#eef4ff", padding=8)
        s.map("TButton", background=[("active","#263850")])
        s.configure("Accent.TButton", background="#31c9dc", foreground="#061015")
        s.configure("Danger.TButton", background="#4a1822", foreground="#ffb7c2")
        s.configure("TEntry", fieldbackground="#0a111a", foreground="#eef4ff")
        s.configure("TCombobox", fieldbackground="#0a111a", foreground="#eef4ff")
        # The read-only dropdowns would otherwise fall back to clam's light grey with light text.
        s.map("TCombobox", fieldbackground=[("readonly","#0a111a")], foreground=[("readonly","#eef4ff")],
              selectbackground=[("readonly","#0a111a")], selectforeground=[("readonly","#eef4ff")],
              background=[("readonly","#1b2638"),("active","#263850")], arrowcolor=[("readonly","#7ee7ff")])
        for opt,val in (("background","#0a111a"),("foreground","#eef4ff"),("selectBackground","#203249"),("selectForeground","#eef4ff")):
            self.option_add(f"*TCombobox*Listbox.{opt}",val)
        s.configure("Horizontal.TScale", background="#31c9dc", troughcolor="#0a111a", bordercolor="#28364d")
        s.configure("TCheckbutton", background="#0f1621", foreground="#dce7f7", indicatorbackground="#0a111a", indicatorforeground="#7ee7ff")
        s.map("TCheckbutton", background=[("active","#0f1621")], indicatorbackground=[("selected","#0a111a"),("pressed","#0a111a")])
        s.configure("Horizontal.TProgressbar", troughcolor="#080d14", background="#52e7ff")

    def _build(self):
        top=ttk.Frame(self); top.pack(fill="x",padx=12,pady=(10,4))
        ttk.Label(top,text="WAVEFORGE VIDEO",font=("Segoe UI",22,"bold"),foreground="#5de7ff").pack(side="left")
        ttk.Label(top,text=f"  v{__version__}  •  trim • convert • upscale",foreground="#91a2bb").pack(side="left",pady=8)
        ttk.Button(top,text="Open Video",style="Accent.TButton",command=self.open_file).pack(side="right",padx=4)
        ttk.Button(top,text="FFmpeg Settings",command=self.settings).pack(side="right",padx=4)

        body=ttk.Panedwindow(self,orient="horizontal"); body.pack(fill="both",expand=True,padx=12,pady=8)

        left=ttk.Frame(body,width=270); center=ttk.Frame(body); right=ttk.Frame(body,width=320)
        body.add(left,weight=0); body.add(center,weight=1); body.add(right,weight=0)

        lf=ttk.Labelframe(left,text="SOURCE"); lf.pack(fill="x",pady=4)
        self.file_lbl=ttk.Label(lf,text="No file loaded",wraplength=235); self.file_lbl.pack(anchor="w",padx=8,pady=8)
        self.meta=ttk.Label(lf,text="",wraplength=235,foreground="#8fa1ba"); self.meta.pack(anchor="w",padx=8,pady=(0,8))

        edit=ttk.Labelframe(left,text="EDIT"); edit.pack(fill="x",pady=6)
        ttk.Label(edit,text="Trim start (seconds)").pack(anchor="w",padx=8,pady=(8,2))
        self.trim_start=tk.StringVar(value="0"); ttk.Entry(edit,textvariable=self.trim_start).pack(fill="x",padx=8)
        ttk.Label(edit,text="Trim end (seconds, blank = end)").pack(anchor="w",padx=8,pady=(8,2))
        self.trim_end=tk.StringVar(value=""); ttk.Entry(edit,textvariable=self.trim_end).pack(fill="x",padx=8)
        self.mute=tk.BooleanVar(value=False); ttk.Checkbutton(edit,text="Mute audio",variable=self.mute).pack(anchor="w",padx=8,pady=6)
        ttk.Label(edit,text="Volume").pack(anchor="w",padx=8)
        self.volume=tk.DoubleVar(value=1.0); ttk.Scale(edit,from_=0,to=2,variable=self.volume,orient="horizontal").pack(fill="x",padx=8,pady=(0,8))
        self.rotate=tk.StringVar(value="None")
        ttk.Label(edit,text="Rotate").pack(anchor="w",padx=8)
        ttk.Combobox(edit,textvariable=self.rotate,state="readonly",values=["None","90° clockwise","90° counterclockwise","180°"]).pack(fill="x",padx=8,pady=(2,8))

        shots=ttk.Labelframe(center,text="FRAMES AT THE TRIM POINTS"); shots.pack(fill="x",pady=4)
        self.frame_cv={};self.frame_cap={}
        for col,slot in enumerate(("start","end")):
            shots.columnconfigure(col,weight=1,uniform="shots")
            cell=ttk.Frame(shots); cell.grid(row=0,column=col,sticky="ew",padx=8,pady=8)
            self.frame_cap[slot]=ttk.Label(cell,text=slot.upper(),foreground="#8fa1ba"); self.frame_cap[slot].pack(anchor="w")
            cv=tk.Canvas(cell,bg="#070b11",highlightthickness=0,width=10,height=self.preview_box[1]); cv.pack(fill="x",pady=(2,0))
            cv.bind("<Configure>",lambda e,s=slot:self._frame_panel_resized(s,e.width))
            self.frame_cv[slot]=cv
            self._frame_content[slot]=("text","Open a video")

        preview=ttk.Labelframe(center,text="TIMELINE"); preview.pack(fill="both",expand=True,pady=4)
        self.canvas=tk.Canvas(preview,bg="#070b11",highlightthickness=0)
        self.canvas.pack(fill="both",expand=True,padx=8,pady=8)
        self.canvas.bind("<Configure>",lambda e:self.draw_timeline())
        self.canvas.bind("<Button-1>",lambda e:self.timeline_click(e,"start"))
        self.canvas.bind("<Button-3>",lambda e:self.timeline_click(e,"end"))
        self.canvas.bind("<Motion>",self._on_hover)
        self.canvas.bind("<Leave>",self._on_leave)
        for var in (self.trim_start,self.trim_end):
            var.trace_add("write",lambda *a:(self.draw_timeline(),self._schedule_previews()))
        self.rotate.trace_add("write",lambda *a:self._schedule_previews())

        trans=ttk.Frame(center); trans.pack(fill="x",pady=6)
        ttk.Button(trans,text="▶ Play in default player",command=self.preview_external).pack(side="left",padx=3)
        ttk.Button(trans,text="⏹ Cancel Job",style="Danger.TButton",command=self.cancel_job).pack(side="left",padx=3)
        self.time_lbl=ttk.Label(trans,text="00:00 / 00:00",foreground="#8fa1ba"); self.time_lbl.pack(side="right",padx=5)

        export=ttk.Labelframe(right,text="EXPORT / CONVERT"); export.pack(fill="x",pady=4)
        ttk.Label(export,text="Preset").pack(anchor="w",padx=8,pady=(8,2))
        self.preset=tk.StringVar(value="MP4 H.264 (Best compatibility)")
        vals=[
            "MP4 H.264 (Best compatibility)",
            "MP4 H.265 (Smaller file)",
            "WebM VP9",
            "MOV H.264",
            "Audio only MP3",
            "Audio only FLAC"
        ]
        ttk.Combobox(export,textvariable=self.preset,state="readonly",values=vals).pack(fill="x",padx=8)
        ttk.Label(export,text="Resolution").pack(anchor="w",padx=8,pady=(8,2))
        self.resolution=tk.StringVar(value="Keep source")
        ttk.Combobox(export,textvariable=self.resolution,state="readonly",values=["Keep source","720p","1080p","1440p","4K UHD (3840×2160)"]).pack(fill="x",padx=8)
        ttk.Label(export,text="4K enhancement").pack(anchor="w",padx=8,pady=(8,2))
        self.enhance=tk.StringVar(value="Off")
        ttk.Combobox(export,textvariable=self.enhance,state="readonly",values=["Off","Lanczos upscale","Lanczos + light sharpen","Denoise + Lanczos + sharpen"]).pack(fill="x",padx=8)
        ttk.Label(export,text="Quality").pack(anchor="w",padx=8,pady=(8,2))
        self.quality=tk.StringVar(value="High")
        ttk.Combobox(export,textvariable=self.quality,state="readonly",values=["Fast","Balanced","High","Maximum"]).pack(fill="x",padx=8)
        self.hw=tk.BooleanVar(value=bool(self.cfg.get("prefer_hardware",True)))
        self.hw.trace_add("write",lambda *a:self._remember("prefer_hardware",self.hw.get()))
        ttk.Checkbutton(export,text="Use hardware encoder when available",variable=self.hw).pack(anchor="w",padx=8,pady=6)
        ttk.Button(export,text="Choose Output + Start",style="Accent.TButton",command=self.export).pack(fill="x",padx=8,pady=(4,8))

        tools=ttk.Labelframe(right,text="QUICK CONVERT"); tools.pack(fill="x",pady=6)
        ttk.Button(tools,text="Convert to MP4 1080p",command=lambda:self.quick("MP4 H.264 (Best compatibility)","1080p")).pack(fill="x",padx=8,pady=3)
        ttk.Button(tools,text="Enhance / Upscale to 4K",command=lambda:self.quick("MP4 H.264 (Best compatibility)","4K UHD (3840×2160)","Lanczos + light sharpen")).pack(fill="x",padx=8,pady=3)
        ttk.Button(tools,text="Extract MP3",command=lambda:self.quick("Audio only MP3","Keep source")).pack(fill="x",padx=8,pady=(3,8))

        info=ttk.Labelframe(right,text="ENGINE"); info.pack(fill="x",pady=6)
        self.engine=ttk.Label(info,text="Checking FFmpeg...",wraplength=280); self.engine.pack(anchor="w",padx=8,pady=8)

        jobs=ttk.Labelframe(right,text="RECENT JOBS"); jobs.pack(fill="both",expand=True,pady=6)
        self.jobs=tk.Listbox(jobs,bg="#091019",fg="#dfe9f6",selectbackground="#203249",borderwidth=0,highlightthickness=0)
        self.jobs.pack(fill="both",expand=True,padx=8,pady=8)

        bottom=ttk.Frame(self); bottom.pack(fill="x",padx=12,pady=(0,10))
        self.progress=ttk.Progressbar(bottom,mode="determinate"); self.progress.pack(side="left",fill="x",expand=True,padx=(0,8))
        self.status=ttk.Label(bottom,text="Ready",foreground="#91a1b9"); self.status.pack(side="right")

    def _remember(self,key,value):
        self.cfg[key]=value;save_config(self.cfg)

    def _boot_checks(self):
        if not self.ffmpeg or not self.ffprobe:
            self.engine.config(text="FFmpeg/ffprobe not found.\nInstall it with:  winget install Gyan.FFmpeg\nor point to it under FFmpeg Settings, or put ffmpeg.exe and ffprobe.exe in a bin folder beside this app.")
            return
        listed=detect_encoders(self.ffmpeg)
        # Software encoders are usable straight away; hardware ones only once a test encode succeeds.
        self.encoders={e for e in listed if not is_hw_encoder(e)}
        hw_listed=sorted(e for e in listed if is_hw_encoder(e))
        self._enc_gen+=1
        self._show_engine(checking=bool(hw_listed))
        if hw_listed:
            gen,ffmpeg=self._enc_gen,self.ffmpeg
            def verify():
                working={e for e in hw_listed if verify_encoder(ffmpeg,e)}
                self.events.put(("encoders",gen,working))
            threading.Thread(target=verify,daemon=True).start()
        self.cfg["ffmpeg_path"]=self.ffmpeg;self.cfg["ffprobe_path"]=self.ffprobe;save_config(self.cfg)

    def _show_engine(self,checking=False):
        hw=sorted(e for e in self.encoders if is_hw_encoder(e))
        txt="checking..." if checking else (', '.join(hw) if hw else 'none available (using software encoding)')
        self.engine.config(text=f"FFmpeg: {self.ffmpeg}\nHardware encoders: {txt}")

    def open_file(self):
        path=filedialog.askopenfilename(title="Open video/audio",filetypes=MEDIA_FILETYPES)
        if not path:return
        if not self.ffprobe:
            messagebox.showerror("FFmpeg missing","ffprobe was not found. See the ENGINE panel for how to install FFmpeg.")
            return
        try:
            self.media=probe_media(self.ffprobe,path)
        except Exception as e:
            messagebox.showerror("Could not read file",str(e));return
        self.file_lbl.config(text=Path(path).name)
        self.meta.config(text=f"{self.media.width}×{self.media.height}  •  {self.media.fps:.2f} fps\nVideo: {self.media.video_codec or 'none'}  Audio: {self.media.audio_codec or 'none'}\n{self.media.container}\nDuration: {self.media.duration:.2f}s")
        self.wave=None
        if self.media.audio_codec and self.ffmpeg:
            ffmpeg=self.ffmpeg
            threading.Thread(target=lambda:self.events.put(("waveform",path,compute_waveform(ffmpeg,path))),daemon=True).start()
        self.frames.clear_cache();self.frames.cancel("hover")  # the file may have changed since it was last opened
        self._hover_img=None;self.shown.clear()
        self.trim_start.set("0");self.trim_end.set("")
        self.draw_timeline()
        self._refresh_previews()
        self.status.config(text="Media loaded")

    def _trim_values(self):
        """(start, end) seconds as drawn on the timeline; tolerant of half-typed text."""
        dur=self.media.duration
        try:a=float(self.trim_start.get() or 0)
        except ValueError:a=0.0
        try:b=float(self.trim_end.get().strip() or dur)
        except ValueError:b=dur
        a=max(0.0,min(dur,a));b=max(a,min(dur,b))
        return a,b

    def draw_timeline(self):
        c=self.canvas;c.delete("all")
        w=max(10,c.winfo_width());h=max(10,c.winfo_height())
        c.create_rectangle(0,0,w,h,fill="#070b11",outline="")
        mid=h/2
        c.create_line(0,mid,w,mid,fill="#243047")
        if not self.media:
            c.create_text(w/2,mid,text="Open a video to begin",fill="#71819a",font=("Segoe UI",18,"bold"))
            self.time_lbl.config(text="00:00 / 00:00");return
        dur=self.media.duration
        if not self.media.audio_codec:note="No audio track"
        elif self.wave is None:note="Analyzing audio..."
        elif not self.wave:note="Waveform unavailable"
        else:note=""
        if note:
            c.create_text(w/2,mid-24,text=note,fill="#71819a",font=("Segoe UI",12))
        else:
            n=len(self.wave)
            for x in range(0,int(w),2):
                amp=max(1,self.wave[min(n-1,int(x/w*n))]*h*.36)
                c.create_line(x,mid-amp,x,mid+amp,fill="#49cfe2")
        c.create_text(12,15,text=f"{Path(self.media.path).name}   {dur:.1f}s",anchor="w",fill="#b9c8dc")
        c.create_text(w-12,15,text="click: set start   right-click: set end   hover: preview frame",anchor="e",fill="#5d6c85",font=("Segoe UI",9))
        if dur>0:
            step=next((s for s in (1,2,5,10,15,30,60,120,300,600,900,1800,3600,7200) if dur/s<=max(1,w/80)),7200)
            for t in range(0,int(dur)+1,step):
                x=t/dur*w
                c.create_line(x,h-14,x,h,fill="#3a4a66")
                c.create_text(x+3,h-8,text=fmt_time(t),anchor="w",fill="#71819a",font=("Segoe UI",8))
            a,b=self._trim_values()
            x1=a/dur*w;x2=b/dur*w
            c.create_rectangle(0,0,x1,h,fill="#05070a",stipple="gray50",outline="")
            c.create_rectangle(x2,0,w,h,fill="#05070a",stipple="gray50",outline="")
            c.create_rectangle(x1,0,x2,h,outline="#5de7ff",width=2)
            self.time_lbl.config(text=f"{fmt_time(a)} – {fmt_time(b)}   /   {fmt_time(dur)}")
        self._draw_hover()

    def timeline_click(self,e,which):
        if not self.media or not self.media.duration:return
        w=max(1,self.canvas.winfo_width())
        t=min(self.media.duration,max(0.0,e.x/w*self.media.duration))
        if which=="start":
            self.trim_start.set(f"{t:.2f}")
            try:
                if self.trim_end.get().strip() and float(self.trim_end.get())<=t:self.trim_end.set("")
            except ValueError:pass
        else:
            try:start=float(self.trim_start.get() or 0)
            except ValueError:start=0.0
            if t>start:self.trim_end.set(f"{t:.2f}")
            else:self.status.config(text="Trim end must be after trim start")

    # ---- frame previews: START / END panels and the thumbnail under the cursor

    def _can_preview(self):
        return bool(self.media and self.media.video_codec and self.ffmpeg)

    def _schedule_previews(self):
        if self._preview_after:self.after_cancel(self._preview_after)
        self._preview_after=self.after(200,self._refresh_previews)  # wait for typing to settle

    def _refresh_previews(self):
        if self._preview_after:self.after_cancel(self._preview_after)
        self._preview_after=None
        if not self._can_preview():
            msg="Open a video" if not self.media else "No video track" if not self.media.video_codec else "FFmpeg not found"
            for slot in ("start","end"):
                self.frames.cancel(slot);self.shown.pop(slot,None)
                self.frame_cap[slot].config(text=slot.upper());self._set_frame(slot,("text",msg))
            return
        a,b=self._trim_values()
        bw,bh=self.preview_box
        for slot,point,t in (("start",a,a),("end",b,max(a,b-0.04))):  # the last frame kept is just before the end point
            self.frame_cap[slot].config(text=f"{slot.upper()}  {point:.2f}s")
            self.frames.request(slot,(self.media.path,round(preview_time(t,self.media.duration),2),self.rotate.get(),bw,bh))

    def _set_frame(self,slot,content):
        self._frame_content[slot]=content
        self._draw_frame(slot)

    def _draw_frame(self,slot):
        cv=self.frame_cv[slot];cv.delete("all")
        w=max(10,cv.winfo_width());h=max(10,cv.winfo_height())
        kind,value=self._frame_content[slot]
        if kind=="img":cv.create_image(w/2,h/2,image=value)
        else:cv.create_text(w/2,h/2,text=value,fill="#71819a",font=("Segoe UI",11))

    def _frame_panel_resized(self,slot,width):
        self._draw_frame(slot)
        if slot!="start":return
        bw=min(400,max(120,width))//8*8;bh=bw*9//16
        if abs(bw-self.preview_box[0])>=8:
            self.preview_box=(bw,bh)
            for cv in self.frame_cv.values():cv.configure(height=bh)
            self._schedule_previews()

    def _frame_ready(self,slot,key,data):
        self.shown[slot]=key
        try:img=tk.PhotoImage(data=base64.b64encode(data)) if data else None
        except tk.TclError:img=None
        if slot=="hover":
            self._hover_img=img;self._draw_hover()
        else:
            self._set_frame(slot,("img",img) if img else ("text","No frame at this time"))

    def _on_hover(self,e):
        if not self._can_preview() or not self.media.duration:return
        self._hover_x=e.x
        self._draw_hover()
        if self._hover_after:self.after_cancel(self._hover_after)
        self._hover_after=self.after(100,self._request_hover)  # only fetch once the mouse slows down

    def _hover_time(self):
        w=max(1,self.canvas.winfo_width())
        return min(self.media.duration,max(0.0,self._hover_x/w*self.media.duration))

    def _request_hover(self):
        self._hover_after=None
        if self._hover_x is None or not self._can_preview():return
        t=preview_time(round(self._hover_time()*4)/4,self.media.duration)  # quarter-second steps keep the cache useful
        self.frames.request("hover",(self.media.path,t,self.rotate.get(),160,90))

    def _on_leave(self,e):
        if self._hover_after:self.after_cancel(self._hover_after);self._hover_after=None
        self._hover_x=None;self._hover_img=None
        self.frames.cancel("hover");self.shown.pop("hover",None)
        self.canvas.delete("hover")

    def _draw_hover(self):
        c=self.canvas;c.delete("hover")
        if self._hover_x is None or not self.media or not self.media.duration:return
        w=max(10,c.winfo_width());h=max(10,c.winfo_height())
        x=min(max(self._hover_x,0),w);t=self._hover_time()
        c.create_line(x,0,x,h,fill="#8fa1ba",dash=(2,3),tags="hover")
        img=self._hover_img
        iw,ih=(img.width(),img.height()) if img else (0,0)
        cx=min(max(x,iw/2+6),w-iw/2-6)  # keep the thumbnail inside the canvas
        if img:
            c.create_rectangle(cx-iw/2-2,28,cx+iw/2+2,30+ih+2,fill="#0a111a",outline="#5de7ff",tags="hover")
            c.create_image(cx,30,image=img,anchor="n",tags="hover")
        c.create_text(cx,30+ih+(12 if img else 4),text=f"{fmt_time(t)}.{int(t%1*10)}",fill="#eef4ff",font=("Segoe UI",9,"bold"),tags="hover")

    def preview_external(self):
        if not self.media:return
        try:
            if os.name=="nt":os.startfile(self.media.path)
            else:subprocess.Popen(["xdg-open",self.media.path])
        except Exception as e:messagebox.showerror("Preview failed",str(e))

    def settings(self):
        win=tk.Toplevel(self);win.title("FFmpeg Settings");win.geometry("640x230");win.configure(bg="#0f1621")
        win.columnconfigure(0,weight=1)
        f=tk.StringVar(value=self.ffmpeg or self.cfg.get("ffmpeg_path",""))
        p=tk.StringVar(value=self.ffprobe or self.cfg.get("ffprobe_path",""))
        def browse(var):
            chosen=filedialog.askopenfilename(parent=win,title="Select executable",filetypes=[("Programs","*.exe"),("All files","*.*")])
            if chosen:var.set(chosen)
        for row,(label,var) in enumerate((("FFmpeg path",f),("ffprobe path",p))):
            ttk.Label(win,text=label).grid(row=row*2,column=0,columnspan=2,sticky="w",padx=10,pady=(10,2))
            ttk.Entry(win,textvariable=var).grid(row=row*2+1,column=0,sticky="ew",padx=(10,4))
            ttk.Button(win,text="Browse...",command=lambda v=var:browse(v)).grid(row=row*2+1,column=1,padx=(0,10))
        def save():
            self.cfg["ffmpeg_path"]=f.get().strip();self.cfg["ffprobe_path"]=p.get().strip();save_config(self.cfg)
            self.ffmpeg=find_executable("ffmpeg.exe",self.cfg["ffmpeg_path"]) or find_executable("ffmpeg",self.cfg["ffmpeg_path"])
            self.ffprobe=find_executable("ffprobe.exe",self.cfg["ffprobe_path"]) or find_executable("ffprobe",self.cfg["ffprobe_path"])
            self._boot_checks();win.destroy()
        ttk.Button(win,text="Save",style="Accent.TButton",command=save).grid(row=4,column=0,columnspan=2,pady=14)

    def quick(self,preset,res,enhance="Off"):
        self.preset.set(preset);self.resolution.set(res);self.enhance.set(enhance);self.export()

    def export(self):
        if not self.media:return messagebox.showwarning("No media","Open a video or audio file first.")
        if not self.ffmpeg:return messagebox.showerror("FFmpeg missing","FFmpeg was not found.")
        preset=self.preset.get()
        ext=".mp4"
        if preset=="WebM VP9":ext=".webm"
        elif preset=="MOV H.264":ext=".mov"
        elif preset=="Audio only MP3":ext=".mp3"
        elif preset=="Audio only FLAC":ext=".flac"
        # Check the settings before asking where to save, so a bad combination fails fast.
        try:
            self._make_job("x"+ext)
        except Exception as e:return messagebox.showerror("Export settings",str(e))
        stem=Path(self.media.path).stem+"_waveforge"
        last_dir=self.cfg.get("last_output_dir") or ""
        output=filedialog.asksaveasfilename(initialdir=last_dir if Path(last_dir).is_dir() else default_output_dir(),initialfile=stem+ext,defaultextension=ext)
        if not output:return
        if same_file(output,self.media.path):
            return messagebox.showerror("Export settings","The output would overwrite the source video. Choose a different file name.")
        self.cfg["last_output_dir"]=str(Path(output).parent);save_config(self.cfg)
        try:
            job=self._make_job(output)
        except Exception as e:return messagebox.showerror("Export settings",str(e))
        self.start_job(job)

    def _make_job(self,output):
        """Build the export Job for the current settings, with a software-encoder fallback when a GPU encoder is used."""
        start,end=self._trim_range()
        work=partial_path(output)
        cmd=self.build_command(work)
        vcodec=cmd[cmd.index("-c:v")+1] if "-c:v" in cmd else ""
        fallback=self.build_command(work,force_software=True) if is_hw_encoder(vcodec) else None
        label=f"{Path(self.media.path).name} → {Path(output).name}"
        return Job(cmd,output,label,total=end-start,fallback_cmd=fallback,work_path=work)

    def _trim_range(self):
        start=max(0,float(self.trim_start.get() or 0))
        end_txt=self.trim_end.get().strip()
        end=float(end_txt) if end_txt else self.media.duration
        end=min(self.media.duration,end) if self.media.duration else end
        if end<=start:raise ValueError("Trim end must be after trim start.")
        return start,end

    def build_command(self,output,force_software=False):
        p=self.media.path
        start,end=self._trim_range()
        use_hw=self.hw.get() and not force_software
        preset=self.preset.get()
        audio_only=preset.startswith("Audio only")
        has_audio=bool(self.media.audio_codec)
        if audio_only and not has_audio:raise ValueError("This file has no audio track to export.")
        if audio_only and self.mute.get():raise ValueError("Audio-only export with 'Mute audio' checked would be silent. Uncheck Mute audio.")
        cmd=[self.ffmpeg,"-y","-hide_banner","-ss",f"{start:.3f}","-i",p,"-t",f"{end-start:.3f}"]
        has_video=bool(self.media.video_codec) and not audio_only
        filters=[]
        if has_video:
            if self.rotate.get() in ROTATE_FILTERS:filters.append(ROTATE_FILTERS[self.rotate.get()])

            res=self.resolution.get()
            dims={"720p":(1280,720),"1080p":(1920,1080),"1440p":(2560,1440),"4K UHD (3840×2160)":(3840,2160)}
            enh=self.enhance.get()
            if res in dims:
                w,h=dims[res]
                if enh=="Denoise + Lanczos + sharpen":
                    filters += ["hqdn3d=1.5:1.5:6:6",f"scale={w}:{h}:force_original_aspect_ratio=decrease:flags=lanczos",f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2","unsharp=5:5:0.55:5:5:0.0"]
                elif enh=="Lanczos + light sharpen":
                    filters += [f"scale={w}:{h}:force_original_aspect_ratio=decrease:flags=lanczos",f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2","unsharp=5:5:0.35:5:5:0.0"]
                else:
                    filters += [f"scale={w}:{h}:force_original_aspect_ratio=decrease:flags=lanczos",f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2"]
            else:
                if enh!="Off":
                    # enhancement without forced resolution
                    filters.append("unsharp=5:5:0.35:5:5:0.0")
                filters.append("scale=trunc(iw/2)*2:trunc(ih/2)*2")  # H.264/H.265 need even dimensions

        if filters:cmd += ["-vf",",".join(filters)]

        if self.mute.get():cmd += ["-an"]
        elif has_audio:
            vol=float(self.volume.get())
            if abs(vol-1.0)>.01:cmd += ["-af",f"volume={vol:.3f}"]

        q=self.quality.get()
        hw_args=lambda enc,cq:(["-preset","p5","-rc","vbr","-cq",cq,"-b:v","0"] if "nvenc" in enc
                               else ["-global_quality",cq] if "qsv" in enc
                               else ["-quality","quality" if cq=="20" else "balanced"])
        if preset=="MP4 H.264 (Best compatibility)" or preset=="MOV H.264":
            enc=choose_encoder(self.encoders,"h264",use_hw)
            cmd += ["-c:v",enc,"-pix_fmt","yuv420p"]
            if enc=="libx264":
                crf={"Fast":"23","Balanced":"20","High":"18","Maximum":"16"}[q]
                cmd += ["-preset",{"Fast":"veryfast","Balanced":"medium","High":"slow","Maximum":"slower"}[q],"-crf",crf]
            else:
                cmd += hw_args(enc,"20" if q in ("High","Maximum") else "24")
            if not self.mute.get():cmd += ["-c:a","aac","-b:a","192k"]
            if output.lower().endswith(".mp4"):cmd += ["-movflags","+faststart"]
        elif preset=="MP4 H.265 (Smaller file)":
            enc=choose_encoder(self.encoders,"h265",use_hw)
            cmd += ["-c:v",enc,"-pix_fmt","yuv420p","-tag:v","hvc1"]
            if enc=="libx265":cmd += ["-crf",{"Fast":"28","Balanced":"25","High":"22","Maximum":"20"}[q],"-preset",{"Fast":"veryfast","Balanced":"medium","High":"slow","Maximum":"slower"}[q]]
            else:cmd += hw_args(enc,"24")
            if not self.mute.get():cmd += ["-c:a","aac","-b:a","192k"]
            cmd += ["-movflags","+faststart"]
        elif preset=="WebM VP9":
            cmd += ["-c:v","libvpx-vp9","-pix_fmt","yuv420p","-crf",{"Fast":"36","Balanced":"32","High":"28","Maximum":"24"}[q],"-b:v","0"]
            if not self.mute.get():cmd += ["-c:a","libopus","-b:a","160k"]
        elif preset=="Audio only MP3":
            cmd += ["-vn","-c:a","libmp3lame","-b:a","192k"]
        elif preset=="Audio only FLAC":
            cmd += ["-vn","-c:a","flac"]
        return cmd + ["-progress","pipe:1","-nostats",output]

    def start_job(self,job):
        if self.current_job:return messagebox.showwarning("Job running","Cancel or wait for the current job.")
        self.current_job=job;self.progress["value"]=0;self.status.config(text="Starting export...")
        self.jobs.insert(0,"RUNNING  "+job.label)
        threading.Thread(target=self._worker,args=(job,),daemon=True).start()

    def _run_ffmpeg(self,job,cmd):
        """Run one ffmpeg process, feeding the progress bar; returns (returncode, stderr tail)."""
        proc=job.proc=subprocess.Popen(cmd,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,encoding="utf-8",errors="replace",bufsize=1,**_NO_WINDOW,**_OWN_GROUP)
        if job.cancelled:job.cancel()  # cancelled before the process existed
        err_lines=collections.deque(maxlen=2000)
        # stderr has to be drained while ffmpeg runs, or a full pipe stalls the encode
        drain=threading.Thread(target=lambda:err_lines.extend(proc.stderr),daemon=True);drain.start()
        total=max(.001,job.total)
        for line in proc.stdout:
            line=line.strip()
            if line.startswith("out_time_ms="):
                try:self.events.put(("progress",max(0,min(100,int(line.split("=",1)[1])/1_000_000/total*100))))
                except ValueError:pass
        proc.wait();drain.join(timeout=5)
        return proc.returncode,"".join(err_lines)

    def _worker(self,job):
        log=LOG_DIR/f"job_{int(time.time()*1000)}.log"
        try:
            runs=[(job.cmd,)+self._run_ffmpeg(job,job.cmd)]
            if runs[-1][1]!=0 and not job.cancelled and job.fallback_cmd:
                self.events.put(("note","Hardware encoder failed - retrying with software encoder..."))
                self.events.put(("progress",0))
                runs.append((job.fallback_cmd,)+self._run_ffmpeg(job,job.fallback_cmd))
            cmd,rc,err=runs[-1]
            self._write_log(log,runs)
            if job.cancelled:
                job.discard_partial()
                self.events.put(("done",False,"Cancelled",job))
            elif rc==0 and Path(job.work_path).exists():
                try:
                    if job.work_path!=job.output:os.replace(job.work_path,job.output)
                except OSError as e:
                    job.discard_partial()
                    self.events.put(("done",False,f"Could not save {job.output}:\n{e}",job))
                else:self.events.put(("done",True,job.output,job))
            else:
                job.discard_partial()
                self.events.put(("done",False,err[-1500:] or "FFmpeg failed",job))
        except Exception as e:
            job.discard_partial()
            self.events.put(("done",False,str(e),job))

    @staticmethod
    def _write_log(log,runs,keep=50):
        try:
            LOG_DIR.mkdir(parents=True,exist_ok=True)
            log.write_text("\n\n".join(f"COMMAND:\n{' '.join(c)}\n\nEXIT CODE: {r}\n\nSTDERR:\n{e}" for c,r,e in runs),encoding="utf-8")
            for old in sorted(LOG_DIR.glob("job_*.log"))[:-keep]:
                old.unlink()
        except OSError:pass

    def cancel_job(self):
        if self.current_job:self.current_job.cancel()

    def _on_close(self):
        job=self.current_job
        if job:
            if not messagebox.askyesno("Export running","An export is still running. Cancel it and quit?"):return
            job.cancel()
            try:job.proc.wait(timeout=5)
            except Exception:pass
            job.discard_partial()
        self.destroy()

    def _poll(self):
        try:
            while True:
                e=self.events.get_nowait()
                if e[0]=="progress":
                    self.progress["value"]=e[1];self.status.config(text=f"Exporting... {e[1]:.0f}%")
                elif e[0]=="note":
                    self.status.config(text=e[1])
                elif e[0]=="encoders":
                    if e[1]==self._enc_gen:
                        self.encoders|=e[2];self._show_engine()
                elif e[0]=="frame":
                    if self.frames.current(e[1],e[2]):self._frame_ready(e[1],e[3],e[4])
                elif e[0]=="waveform":
                    if self.media and e[1]==self.media.path:
                        self.wave=e[2];self.draw_timeline()
                elif e[0]=="done":
                    ok,msg,job=e[1],e[2],e[3];self.current_job=None
                    self.progress["value"]=100 if ok else 0
                    self.jobs.delete(0)
                    self.jobs.insert(0,("DONE  " if ok else "FAILED  ")+job.label)
                    if ok:
                        self.status.config(text="Export complete")
                        messagebox.showinfo("Finished",f"Saved:\n{msg}")
                    else:
                        self.status.config(text="Export cancelled" if msg=="Cancelled" else "Export failed")
                        if msg!="Cancelled":messagebox.showerror("Export failed",msg)
        except queue.Empty:pass
        self.after(100,self._poll)

if __name__=="__main__":
    if "--version" in sys.argv:
        print(f"WaveForge Video {__version__}")
    else:
        App().mainloop()
