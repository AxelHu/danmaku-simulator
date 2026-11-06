#!/usr/bin/env python3
"""Targeted browser QA against a running local Sintel example.

No everyday browser profile is opened. A test-only hook captures the otherwise
closed ShadowRoot; an optional GM shim exercises real HTTP source bytes through
the documented GM callback contract, NOT an installed userscript extension.
"""
import argparse
import base64
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import shutil
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[2]
HOOK = r"""
(() => {
  const original = Element.prototype.attachShadow;
  Element.prototype.attachShadow = function (options) {
    const root = original.call(this, options);
    if (this.hasAttribute('data-dms-client')) window.__dmsQaRoot = root;
    return root;
  };
  window.__gmCalls = []; window.__gmDelay = 0;
  window.__documentSweeps=0;window.__canvasDraws=0;
  const selectAll=Document.prototype.querySelectorAll;
  Document.prototype.querySelectorAll=function(selector){if(selector==='*')window.__documentSweeps++;return selectAll.call(this,selector);};
  const fillText=CanvasRenderingContext2D.prototype.fillText;
  CanvasRenderingContext2D.prototype.fillText=function(...args){window.__canvasDraws++;return fillText.apply(this,args);};
  window.__playerWrites = { play:0, pause:0, currentTime:0, playbackRate:0 };
  for (const name of ['play','pause']) {
    const method=HTMLMediaElement.prototype[name];
    HTMLMediaElement.prototype[name]=function(...args){window.__playerWrites[name]++;return method.apply(this,args);};
  }
  for (const name of ['currentTime','playbackRate']) {
    const descriptor=Object.getOwnPropertyDescriptor(HTMLMediaElement.prototype,name);
    Object.defineProperty(HTMLMediaElement.prototype,name,{...descriptor,set(value){window.__playerWrites[name]++;return descriptor.set.call(this,value);}});
  }
})();
"""
SHIM = r"""
(() => {
  function gm(options) {
    window.__gmCalls.push({url:options.url,method:options.method,anonymous:options.anonymous,responseType:options.responseType});
    let stopped=false, rejectPromise;
    const promise=new Promise((resolve,reject)=>{
      rejectPromise=reject;
      setTimeout(async()=>{
        try {
          const data=await window.__dmsQaHttp(options.url);
          if(stopped)return;
          const bytes=Uint8Array.from(atob(data.base64),c=>c.charCodeAt(0));
          const response={status:data.status,finalUrl:data.url,response:bytes.buffer};
          options.onload?.(response);resolve(response);
        } catch(error) { if(!stopped){options.onerror?.({error:String(error)});reject(error);} }
      },window.__gmDelay);
    });
    promise.abort=()=>{stopped=true;options.onabort?.();rejectPromise(new Error('Aborted by test'));};
    // Legacy callback users do not observe this Promise.
    promise.catch(()=>{});
    return promise;
  }
  if (window.__dmsModernGm) window.GM={xmlHttpRequest:gm,registerMenuCommand:()=>1};
  else {window.GM_xmlhttpRequest=gm;window.GM_registerMenuCommand=()=>1;}
})();
"""
SNAPSHOT = r"""() => {
 const root=window.__dmsQaRoot,c=root.getElementById('overlay'),v=document.getElementById('primary')||document.querySelector('video');
 const data=c.getContext('2d').getImageData(0,0,c.width,c.height).data;
 let nonzero=0,hash=2166136261;
 for(let i=3;i<data.length;i+=4){if(data[i])nonzero++;hash=Math.imul(hash^data[i],16777619);}
 const rect=c.getBoundingClientRect(), vr=v?.getBoundingClientRect();
 return {state:document.querySelector('[data-dms-client]').getAttribute('data-state'),status:root.getElementById('status').textContent,
  pixels:nonzero,hash:hash>>>0,hidden:c.hidden,time:v?.currentTime,paused:v?.paused,rate:v?.playbackRate,duration:v?.duration,
  canvas:{x:rect.x,y:rect.y,width:rect.width,height:rect.height},video:vr?{x:vr.x,y:vr.y,width:vr.width,height:vr.height}:null,
  fullscreen:document.fullscreenElement?.id||null,popover:document.querySelector('[data-dms-client]').matches(':popover-open'),
  trackInfo:root.getElementById('trackInfo').textContent,trackLabel:root.getElementById('trackLabel').textContent};
}"""


def get_bytes(url):
    request = urllib.request.Request(url, headers={"Accept": "application/json, application/xml, text/xml"})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return response.status, response.geturl(), response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.geturl(), error.read()


def bridge(url):
    status, final, body = get_bytes(url)
    return {"status": status, "url": final, "base64": base64.b64encode(body).decode()}


def element(page, ident):
    return page.evaluate_handle("(id)=>window.__dmsQaRoot.getElementById(id)", ident).as_element()


def text(page, ident):
    return element(page, ident).text_content()


def wait_state(page, expected):
    page.wait_for_function("(expected)=>document.querySelector('[data-dms-client]').getAttribute('data-state')===expected", arg=expected)


def import_file(page, body, filename="track.json"):
    with page.expect_file_chooser() as chooser:
        element(page, "openFile").click()
    chooser.value.set_files({"name": filename, "mimeType": "application/xml" if filename.endswith(".xml") else "application/json", "buffer": body})
    page.wait_for_timeout(120)


def seek(page, when):
    page.evaluate("(time)=>{const v=document.getElementById('primary');v.pause();v.currentTime=time;}", when)
    page.wait_for_function("(time)=>Math.abs(document.getElementById('primary').currentTime-time)<0.05&&document.getElementById('primary').readyState>=2",arg=when)
    page.wait_for_timeout(100)


def remote(page, url):
    element(page,"url").fill(url)
    element(page,"loadUrl").click()
    page.wait_for_function("()=>!window.__dmsQaRoot.getElementById('loadUrl').disabled")


@contextmanager
def fixture_server(html):
    body=html.encode()
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type","text/html; charset=utf-8")
            self.send_header("Content-Length",str(len(body)))
            self.end_headers();self.wfile.write(body)
        def log_message(self,*args):
            pass
    server=ThreadingHTTPServer(("127.0.0.1",0),Handler)
    worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
    try: yield "http://127.0.0.1:"+str(server.server_port)+"/preview"
    finally: server.shutdown();server.server_close();worker.join(timeout=2)


def setup(page, fixture, source, url, gm=True, modern=False, intercept=True):
    page.add_init_script(HOOK)
    if gm:
        page.expose_function("__dmsQaHttp", bridge)
        page.add_init_script("window.__dmsModernGm="+str(modern).lower()+";")
        page.add_init_script(SHIM)
    if intercept:
        page.route(url.split("?")[0]+"*", lambda route: route.fulfill(status=200, content_type="text/html; charset=utf-8", body=fixture))
    page.goto(url,wait_until="domcontentloaded")
    page.wait_for_function("()=>document.getElementById('primary').readyState>=2",timeout=30000)
    page.add_script_tag(content=source)
    page.wait_for_function("()=>window.__dmsQaRoot&&window.__dmsQaRoot.getElementById('video').options.length===2")
    element(page,"launcher").click()


def run_browser(playwright, engine, args, fixture, source, standard, xml, track_url, catalog_url, output):
    browser_type=getattr(playwright,engine)
    kwargs={"headless":True}
    if engine=="chromium": kwargs.update(executable_path=args.chrome,args=["--no-sandbox","--disable-dev-shm-usage"])
    browser=browser_type.launch(**kwargs)
    context=browser.new_context(viewport={"width":1440,"height":1000},screen={"width":1440,"height":1000},device_scale_factor=1)
    page=context.new_page(); page.set_default_timeout(10000)
    errors=[]; page.on("pageerror",lambda error:errors.append(str(error)))
    record={"engine":engine,"version":browser.version,"transport":"GM contract shim (real anonymous HTTP bytes; no installed extension)","checks":{}}
    def passed(name,value=True): record["checks"][name]=value
    try:
        fixture_url=args.base+"/__userscript_qa__?episode=1"
        setup(page,fixture,source,fixture_url,modern=engine=="firefox")
        assert page.evaluate("window.__gmCalls.length")==0
        passed("no_network_before_explicit_import")
        assert page.evaluate("Object.values(window.__playerWrites).every(x=>x===0)")
        import_file(page,standard)
        wait_state(page,"active")
        assert "65 条" in text(page,"trackInfo")
        assert page.evaluate("Object.values(window.__playerWrites).every(x=>x===0)")
        passed("local_json_real_65_comments_and_no_player_mutation")
        before_sweeps=page.evaluate("window.__documentSweeps")
        page.evaluate("()=>{const box=document.createElement('div');box.hidden=true;for(let i=0;i<80;i++){const p=document.createElement('p');p.textContent='动态网页内容';box.append(p);}document.body.append(box);}")
        page.wait_for_timeout(300)
        assert page.evaluate("window.__documentSweeps")==before_sweeps
        passed("unrelated_DOM_updates_do_not_rescan_whole_document")
        element(page,"density").focus();element(page,"density").press("End")
        seek(page,8.8)
        first=page.evaluate(SNAPSHOT);page.wait_for_timeout(250);second=page.evaluate(SNAPSHOT)
        assert first["pixels"]>200 and first["hash"]==second["hash"] and first["time"]==second["time"],(first,second)
        passed("paused_canvas_frozen",{"pixels":first["pixels"],"hash":first["hash"],"time":first["time"]})
        page.screenshot(path=str(output/f"{engine}-desktop.png"))
        seek(page,24.4);different=page.evaluate(SNAPSHOT)
        assert different["pixels"]>100 and different["hash"]!=first["hash"]
        passed("seek_reconstructs_current_comments")
        page.evaluate("()=>{const v=document.getElementById('primary');v.playbackRate=2;return v.play();}")
        t0=time.monotonic();m0=page.evaluate("document.getElementById('primary').currentTime")
        page.wait_for_timeout(450)
        m1=page.evaluate("document.getElementById('primary').currentTime"); elapsed=time.monotonic()-t0
        page.evaluate("document.getElementById('primary').pause()")
        ratio=(m1-m0)/elapsed
        assert 1.3<ratio<2.7,ratio
        passed("rate_2_follows_media_clock",{"measured_ratio":round(ratio,3),"media_seconds":m1-m0})
        page.evaluate("document.getElementById('primary').playbackRate=1")
        seek(page,8.8)
        element(page,"fullscreen").click()
        page.wait_for_function("()=>document.fullscreenElement?.id==='primary'")
        page.wait_for_timeout(350)
        full=page.evaluate(SNAPSHOT)
        assert not full["hidden"] and full["pixels"]>200 and full["popover"],full
        page.screenshot(path=str(output/f"{engine}-native-fullscreen.png"))
        passed("native_video_fullscreen_popover",full)
        page.evaluate("document.exitFullscreen()");page.wait_for_timeout(200)
        page.locator("#containerFull").click();page.wait_for_function("()=>document.fullscreenElement?.id==='stage'")
        page.wait_for_timeout(200)
        full=page.evaluate(SNAPSHOT)
        assert not full["hidden"] and full["pixels"]>200
        assert page.evaluate("document.querySelector('[data-dms-client]').parentElement.id")== "stage"
        passed("container_fullscreen_reparents_own_overlay",full)
        page.evaluate("document.exitFullscreen()");page.wait_for_timeout(200)
        page.locator("#changeRoute").click();wait_state(page,"pending")
        assert page.evaluate(SNAPSHOT)["hidden"]
        passed("spa_route_change_stops_old_track")
        element(page,"launcher").click()
        element(page,"bind").click();wait_state(page,"active")
        element(page,"video").select_option("2");wait_state(page,"pending")
        passed("multiple_videos_switch_requires_rematch")
        element(page,"video").select_option("1");element(page,"bind").click();wait_state(page,"active")
        page.locator("#replaceVideo").click();wait_state(page,"pending")
        passed("video_element_replacement_stops_old_track")
        page.wait_for_function("()=>document.getElementById('primary').readyState>=2")
        element(page,"bind").click();wait_state(page,"active")
        page.evaluate("(url)=>{const r=window.__dmsQaRoot;r.getElementById('url').value=url;r.getElementById('loadUrl').click();}",catalog_url)
        assert page.evaluate("window.__gmCalls.length")==0
        passed("untrusted_programmatic_click_cannot_start_GM")
        remote(page,catalog_url)
        assert "目录记录" in text(page,"status")
        assert page.evaluate("window.__gmCalls.length")==1
        passed("catalog_read_does_not_auto_fetch_tracks")
        options=element(page,"catalogTrack").evaluate("(s)=>Array.from(s.options).map(x=>({value:x.value,text:x.textContent}))")
        standard_value=next(o["value"] for o in options if "65 条" in o["text"] and "68 条" not in o["text"])
        element(page,"catalogTrack").select_option(standard_value);element(page,"loadCatalogTrack").click()
        page.wait_for_function("()=>!window.__dmsQaRoot.getElementById('loadCatalogTrack').disabled")
        wait_state(page,"active")
        assert "SHA-256 已核对" in text(page,"trackInfo"),text(page,"trackInfo")
        assert page.evaluate("window.__gmCalls.every(c=>c.method==='GET'&&c.anonymous===true&&c.responseType==='arraybuffer')")
        passed("explicit_catalog_track_real_bytes_sha256_and_anonymous_GM")
        remote(page,track_url)
        assert "65 条" in text(page,"trackInfo");wait_state(page,"active")
        passed("direct_remote_JSON")
        import_file(page,xml,"comments.xml")
        assert "XML" in text(page,"trackInfo") and "65 条" in text(page,"trackInfo")
        passed("local_Bilibili_XML_real_export")
        remote(page,track_url.rsplit("/",1)[0]+"/comments.xml")
        assert "XML" in text(page,"trackInfo")
        passed("direct_remote_XML")
        timing={"format":"danmaku-track/v1","metadata":{"label":"时间映射验收","synthetic":True},
          "timeline":{"scale":2,"offset":3},"comments":[{"time":2,"text":"时间映射在第7秒出现","mode":5}]}
        import_file(page,json.dumps(timing,ensure_ascii=False).encode())
        assert element(page,"offset").input_value()=="3" and element(page,"scale").input_value()=="2"
        seek(page,6.8);before=page.evaluate(SNAPSHOT);seek(page,7.2);after=page.evaluate(SNAPSHOT)
        assert before["pixels"]==0 and after["pixels"]>100,(before,after)
        passed("timeline_scale_2_plus_offset_3_starts_at_7_seconds")
        element(page,"enabled").uncheck();assert page.evaluate(SNAPSHOT)["hidden"]
        element(page,"enabled").check();wait_state(page,"active")
        passed("overlay_toggle_preserves_video")
        duration_mismatch=dict(timing,video={"duration":300})
        import_file(page,json.dumps(duration_mismatch).encode())
        wait_state(page,"pending")
        assert "视频 52.2 秒" in text(page,"alignment") and not element(page,"bind").is_disabled()
        element(page,"bind").click();wait_state(page,"active")
        assert "手动匹配" in text(page,"status")
        passed("known_duration_mismatch_requires_explicit_manual_alignment")
        import_file(page,standard)
        seek(page,8.8)
        page.evaluate("document.getElementById('primary').play()")
        page.wait_for_timeout(160)
        page.evaluate("document.getElementById('primary').style.visibility='hidden'")
        page.wait_for_timeout(200)
        draw_count=page.evaluate("window.__canvasDraws")
        page.wait_for_timeout(250)
        assert page.evaluate("window.__canvasDraws")==draw_count
        page.evaluate("()=>{document.getElementById('primary').style.visibility='visible';document.dispatchEvent(new Event('scroll'));}")
        page.wait_for_timeout(160)
        assert page.evaluate("window.__canvasDraws")>draw_count
        passed("hidden_video_stops_canvas_frames_and_scroll_restores_them")
        page.evaluate("window.dispatchEvent(new PageTransitionEvent('pagehide',{persisted:true}))")
        cache_draws=page.evaluate("window.__canvasDraws")
        page.wait_for_timeout(150)
        assert page.evaluate("window.__canvasDraws")==cache_draws
        page.evaluate("window.dispatchEvent(new PageTransitionEvent('pageshow',{persisted:true}))")
        page.wait_for_timeout(150)
        assert page.evaluate("window.__canvasDraws")>cache_draws
        page.evaluate("document.getElementById('primary').pause()")
        passed("BFCache_persisted_event_handlers_resume_rendering", "Synthetic lifecycle events; not a guarantee of browser cache admission")
        mismatch=dict(timing,bindings=[{"site":"web","url":"https://example.org/a-different-video"}])
        import_file(page,json.dumps(mismatch).encode())
        wait_state(page,"pending");assert element(page,"bind").is_disabled() and page.evaluate(SNAPSHOT)["hidden"]
        passed("explicit_binding_mismatch_blocks_manual_override")
        import_file(page,b'<!DOCTYPE i [<!ENTITY x SYSTEM "file:///etc/passwd">]><i><d p="1,1,25,1">&x;</d></i>',"invalid.xml")
        assert "DTD" in text(page,"status")
        passed("XML_DTD_rejected")
        # A route change during an initially unbound request must not auto-apply it.
        page.evaluate("window.__gmDelay=600")
        element(page,"url").fill(track_url);element(page,"loadUrl").click()
        page.evaluate("history.pushState({},'', '?episode=changed-during-read')")
        page.wait_for_function("()=>!window.__dmsQaRoot.getElementById('loadUrl').disabled")
        wait_state(page,"pending")
        assert "读取期间视频已变化" in text(page,"status")
        passed("late_response_after_route_change_stays_unmatched")
        assert page.locator("#siteOverlay").text_content()=="网页原有内容"
        passed("original_site_content_preserved")
        record["console_errors"]=errors
        assert not errors,errors
        record["passed"]=True

        # Fetch fallback is exercised in the actual browser with cross-origin CORS.
        with fixture_server(fixture) as alt:
            fallback=context.new_page();fallback.set_default_timeout(10000)
            fallback.on("pageerror",lambda error:errors.append(str(error)))
            setup(fallback,fixture,source,alt,gm=False,intercept=False)
            remote(fallback,track_url);wait_state(fallback,"active")
            assert "浏览器 fetch" in text(fallback,"status") or "浏览器 fetch" in fallback.evaluate("window.__dmsQaRoot.querySelector('.footer').textContent")
            passed("native_fetch_fallback_cross_origin_catalog_CORS")
            fallback.close()
        # The page is HTTPS while the real track source remains local HTTP.
        # The transport is explicitly a shim, so this does not certify extension permissions.
        secure=context.new_page();secure.set_default_timeout(10000)
        secure.on("pageerror",lambda error:errors.append(str(error)))
        secure_media="https://client-qa.invalid/real-sintel.mp4"
        media_status,_,media_bytes=get_bytes(args.base+"/api/runs/sintel-v2/video")
        assert media_status==200
        secure.route(secure_media,lambda route:route.fulfill(status=200,content_type="video/mp4",body=media_bytes))
        secure_fixture=fixture.replace(args.base+"/api/runs/sintel-v2/video",secure_media)
        setup(secure,secure_fixture,source,"https://client-qa.invalid/preview",modern=engine=="firefox")
        remote(secure,track_url);wait_state(secure,"active")
        assert secure.evaluate("window.__gmCalls.length")==1
        passed("HTTPS_fixture_HTTP_source_via_GM_contract_shim")
        secure.close()
        assert not errors,errors
    except Exception as error:
        record["passed"]=False;record["error"]=str(error);record["traceback"]=traceback.format_exc()
        record["console_errors"]=errors
        try: record["last_state"]=page.evaluate(SNAPSHOT);page.screenshot(path=str(output/f"{engine}-failure.png"))
        except Exception: pass
    finally:
        context.close();browser.close()
    return record


def external_bilibili(playwright,args,source,output):
    browser=playwright.chromium.launch(executable_path=args.chrome,headless=True,args=["--no-sandbox","--disable-dev-shm-usage"])
    context=browser.new_context(viewport={"width":1440,"height":1000})
    page=context.new_page();page.set_default_timeout(10000)
    result={"url":"https://www.bilibili.com/video/BV1iW411j7bn/","scope":"Public logged-out read-only page; no cookies or everyday profile reused"}
    try:
        response=page.goto(result["url"],wait_until="domcontentloaded",timeout=25000)
        result["http_status"]=response.status if response else None
        page.wait_for_timeout(2500)
        result.update(title=page.title(),video_count=page.locator("video").count())
        if not result["video_count"]:
            result["visible_text"]=page.locator("body").inner_text()[:450]
            result["outcome"]="Bilibili returned HTTP 412; no player, overlay, or fullscreen validation possible." if result.get("http_status")==412 or "412" in result["visible_text"] else "No HTML5 video available in this isolated session."
        else:
            result["media"]=page.evaluate("()=>Array.from(document.querySelectorAll('video')).map(v=>({ready:v.readyState,duration:Number.isFinite(v.duration)?v.duration:null,paused:v.paused,source_present:!!v.currentSrc}))")
            result["outcome"]="Video element observed; content alignment with the trailer track was not tested."
        try: page.screenshot(path=str(output/"bilibili-public-page.png"),timeout=2500)
        except Exception as error: result["screenshot_error"]=str(error)
    except Exception as error:
        result.update(outcome="External page could not be validated in this isolated session.",error=str(error))
    finally: context.close();browser.close()
    return result


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--base",default="http://127.0.0.1:8793")
    parser.add_argument("--chrome",default=shutil.which("google-chrome") or shutil.which("chromium"))
    parser.add_argument("--engines",default="firefox,chromium")
    parser.add_argument("--external",action="store_true")
    parser.add_argument("--output",default=".local/browser-qa")
    args=parser.parse_args();args.base=args.base.rstrip("/")
    output=(ROOT/args.output).resolve();output.mkdir(parents=True,exist_ok=True)
    source=(ROOT/"clients/browser/danmaku-simulator.user.js").read_text()
    fixture=(ROOT/"tests/browser/fixture.html").read_text().replace("__MEDIA_URL__",args.base+"/api/runs/sintel-v2/video")
    catalog_url=args.base+"/catalog/catalog.json"
    status,_,catalog_bytes=get_bytes(catalog_url);assert status==200
    catalog=json.loads(catalog_bytes)
    track=next(t for item in catalog["items"] for t in item["tracks"] if t["id"]=="standard")
    track_url=urllib.parse.urljoin(catalog_url,track["formats"]["json"]["href"])
    status,_,standard=get_bytes(track_url);assert status==200
    status,_,xml=get_bytes(urllib.parse.urljoin(catalog_url,track["formats"]["xml"]["href"]));assert status==200
    report={"evidence":"Real Sintel trailer and published source bytes, independent fixture page; GM shim, not a real extension installation.","browsers":[]}
    with sync_playwright() as p:
        for engine in args.engines.split(","):
            result=run_browser(p,engine,args,fixture,source,standard,xml,track_url,catalog_url,output)
            report["browsers"].append(result)
            print(json.dumps({"engine":engine,"passed":result["passed"],"checks":list(result["checks"]),"error":result.get("error")},ensure_ascii=False),flush=True)
        if args.external:
            report["external_bilibili"]=external_bilibili(p,args,source,output)
            print(json.dumps(report["external_bilibili"],ensure_ascii=False),flush=True)
    (output/"report.json").write_text(json.dumps(report,ensure_ascii=False,indent=2)+"\n")
    raise SystemExit(0 if all(r["passed"] for r in report["browsers"]) else 1)


if __name__=="__main__":
    main()
