"""Drag from an external GTK4 file source, matching Nautilus's content provider.
Run under X11 with PyGObject/GTK4, libXtst and xwininfo installed:
    xvfb-run -a -s '-screen 0 1920x1080x24' python3 tests/gtk_drop_smoke.py
Uses only a temporary installation and temporary images.
"""
import ctypes as C,time,sys,tempfile
from pathlib import Path
from unittest.mock import Mock
import tkinter as tk
from tkinter import ttk
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app import App
class Data(C.Union):_fields_=[('b',C.c_char*20),('s',C.c_short*10),('l',C.c_long*5)]
class Client(C.Structure):_fields_=[('type',C.c_int),('serial',C.c_ulong),('send_event',C.c_int),('display',C.c_void_p),('window',C.c_ulong),('message_type',C.c_ulong),('format',C.c_int),('data',Data)]
class Event(C.Union):_fields_=[('client',Client),('pad',C.c_long*24)]
x=C.CDLL('libX11.so.6');x.XOpenDisplay.argtypes=[C.c_char_p];x.XOpenDisplay.restype=C.c_void_p
d=x.XOpenDisplay(None);assert d
x.XInternAtom.argtypes=[C.c_void_p,C.c_char_p,C.c_int];x.XInternAtom.restype=C.c_ulong
x.XSendEvent.argtypes=[C.c_void_p,C.c_ulong,C.c_int,C.c_long,C.POINTER(Event)]
x.XFlush.argtypes=[C.c_void_p]
def atom(name):return x.XInternAtom(d,name.encode(),False)
def send(window,name,values):
 e=Event();e.client.type=33;e.client.display=d;e.client.window=window;e.client.message_type=atom(name);e.client.format=32
 for i,v in enumerate(values):e.client.data.l[i]=v
 x.XSendEvent(d,window,False,0,C.byref(e));x.XFlush(d)
import core
from PIL import Image
fixture=tempfile.TemporaryDirectory();folder=Path(fixture.name)
settings=core.Settings(folder/'installation');settings.ensure()
root=tk.Tk();root.withdraw()
errors=[]
root.report_callback_exception=lambda *args: errors.append(str(args))
app=App(root,settings)
app.error=lambda error: errors.append(str(error))
root.deiconify()
for _ in range(80):root.update();time.sleep(.025)
assert not app.busy
retained = {field: 'retained ' + field for field in core.FIELDS}
retained['category'] = 'RETAINED'
retained['anzahl'] = '7'
app.fill(retained)
retained = app.values()

def pump():
 for _ in range(10):root.update();time.sleep(.02)
try:
 with tempfile.TemporaryDirectory() as tmp:
  path=Path(tmp)/'image.jpg';Image.new('RGB',(50,40),'red').save(path)
  other=Path(tmp)/'other.jpg';Image.new('RGB',(50,40),'blue').save(other)
  import subprocess,re
  tree=subprocess.check_output(['xwininfo','-tree','-id',str(root.winfo_id())],text=True)
  parent=re.search(r'Parent window id: (0x[0-9a-f]+)',tree)[1]
  root.lift();root.update()
  for target, path in [(target, image) for target in [app.preview.winfo_id()] for image in [path, other]]:
   owner=subprocess.Popen([sys.executable,'-c',"import sys, gi\ngi.require_version('Gtk','4.0');gi.require_version('GdkX11','4.0')\nfrom gi.repository import Gtk,Gdk,Gio,GLib,GdkX11\nGtk.init()\nw=Gtk.Window(title='External GTK4 drag source');w.set_default_size(180,100)\nb=Gtk.Button(label='Drag image');w.set_child(b)\nd=Gtk.DragSource();d.set_actions(Gdk.DragAction.COPY)\np=Gdk.ContentProvider.new_for_value(Gdk.FileList.new_from_list([Gio.File.new_for_path(sys.argv[1])]))\nd.connect('prepare',lambda *args:p);b.add_controller(d)\nw.present()\ndef ready():\n print(w.get_surface().get_xid(),flush=True);return False\nGLib.timeout_add(300,ready);GLib.MainLoop().run()\n",str(path)],stdout=subprocess.PIPE,text=True)
   sid=int(owner.stdout.readline())
   x.XMoveWindow.argtypes=[C.c_void_p,C.c_ulong,C.c_int,C.c_int]
   x.XMoveWindow(d,sid,1100,100);x.XFlush(d);pump()
   xt=C.CDLL('libXtst.so.6')
   xt.XTestFakeMotionEvent.argtypes=[C.c_void_p,C.c_int,C.c_int,C.c_int,C.c_ulong]
   xt.XTestFakeButtonEvent.argtypes=[C.c_void_p,C.c_uint,C.c_int,C.c_ulong]
   def motion(mx,my):
    xt.XTestFakeMotionEvent(d,-1,mx,my,0);x.XFlush(d);pump()
   motion(1180,150)
   xt.XTestFakeButtonEvent(d,1,1,0);x.XFlush(d);pump()
   px=app.preview.winfo_rootx()+200;py=app.preview.winfo_rooty()+100
   for step in range(1,11): motion(int(1180+(px-1180)*step/10),int(150+(py-150)*step/10))
   xt.XTestFakeButtonEvent(d,1,0,0);x.XFlush(d);pump()
   owner.terminate();owner.wait(timeout=5)
   assert app.media==[path], (target, app.media, errors)
   assert app.values()==retained, (app.values(), retained)
   assert app.preview.original is not None, app.status.get()
   pixel=app.preview.original.getpixel((0,0))
   assert (pixel[0] > pixel[2]) == (path.name == 'image.jpg'), (path, pixel)
 print('PASS: consecutive mouse drops from a separate GTK4 application open and render both images.')
finally:app.executor.shutdown(wait=True);root.destroy();fixture.cleanup()
