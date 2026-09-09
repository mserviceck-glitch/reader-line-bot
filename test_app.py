import unittest, json, base64, hmac, hashlib, threading, os, urllib.request, urllib.error
from unittest.mock import patch
import app

def event(text=None, image=None, user='one'):
    return {'type':'message','source':{'type':'user','userId':user},'replyToken':'test',
            'message':{'type':'image','id':image} if image else {'type':'text','text':text}}

def record():
    return {**{k:None for k in app.FIELDS},'color':'เทา','status':'ok'}

class Tests(unittest.TestCase):
    def setUp(self):
        self.sent=[]; self.calls=[]; self.now=100000
        def reader(ids):
            self.calls.append(list(ids)); return record()
        self.bot=app.Bot(lambda e,t,**kw:self.sent.append((e['source']['userId'],t,kw)),reader,lambda:self.now)
    def test_multi_and_isolation(self):
        self.bot.handle(event(image='1')); self.bot.handle(event(image='2'))
        self.bot.handle(event(image='3',user='two'))
        self.bot.handle(event('อ่านข้อมูล'))
        self.assertEqual(self.calls,[['1','2']]); self.assertTrue(self.sent[-1][2]['push'])
        self.assertNotIn('one',self.bot.sessions)
        self.bot.handle(event('อ่านข้อมูล')); self.assertEqual(len(self.calls),1)
        self.assertEqual(self.bot.sessions['two']['images'],['3'])
    def test_ai_notice_before_reading(self):
        self.bot.handle(event(image='1'))
        self.assertTrue(self.sent[-1][1].startswith(app.NOTICE))
        self.assertFalse(self.calls)
    def test_read_clears_then_next_car(self):
        self.bot.handle(event(image='1')); self.bot.handle(event('อ่านข้อมูล'))
        self.assertNotIn('one',self.bot.sessions)
        self.bot.handle(event(image='2')); self.bot.handle(event('อ่าน'))
        self.assertEqual(self.calls,[['1'],['2']])
        self.assertNotIn('one',self.bot.sessions)
    def test_manual_clear_and_old_menu(self):
        self.bot.handle(event(image='1')); self.bot.handle(event('ล้าง'))
        self.assertNotIn('one',self.bot.sessions)
        self.bot.handle(event('ยืนยัน'))
        self.assertNotIn('one',self.bot.sessions)
        self.assertNotIn('ยืนยัน',self.sent[-1][1])
    def test_result_contains_only_form_and_notice(self):
        data={**record(),'title':'นาง','first_name':'ตัวอย่าง','house_number':'7/1',
              'village_number':'6','address_province':'เชียงราย',
              'registration_province':'พะเยา','vin':'MRO0010','seats':'0',
              'warnings':['should not be shown']}
        result=app.render(data)
        self.assertTrue(result.startswith(app.NOTICE))
        self.assertIn('จังหวัด: เชียงราย',result)
        self.assertIn('จังหวัดทะเบียน: พะเยา',result)
        self.assertIn('เลขตัวถัง: MRO0010',result)
        self.assertIn('ที่นั่ง: 0',result)
        self.assertIn('รหัสไปรษณีย์: -',result)
        for phrase in ('จุดที่ต้องตรวจ','should not be shown','ยืนยัน','แก้ไข','วันจดทะเบียน','ใช้ข้อมูล:'):
            self.assertNotIn(phrase,result)
    def test_failed_delivery_retains_result_for_retry(self):
        sent=self.bot.send
        def failing(e,t,**kw):
            if kw.get('push'): raise RuntimeError('simulated')
            sent(e,t,**kw)
        self.bot.send=failing
        self.bot.handle(event(image='1'))
        with self.assertRaises(RuntimeError): self.bot.handle(event('อ่านข้อมูล'))
        self.assertIsNotNone(self.bot.sessions['one']['record'])
        self.bot.send=sent
        self.bot.handle(event('อ่านข้อมูล'))
        self.assertEqual(len(self.calls),1)
        self.assertNotIn('one',self.bot.sessions)
    def test_quick_reply_has_camera_read_clear(self):
        with patch.dict(os.environ,LINE_CHANNEL_ACCESS_TOKEN='fake'),patch.object(app,'api') as call:
            app.line_send(event('help'),'sample')
        message=call.call_args.args[2]['messages'][-1]
        actions=[x['action'] for x in message['quickReply']['items']]
        self.assertEqual(actions[0]['type'],'cameraRoll')
        self.assertEqual([a.get('text') for a in actions[1:]],['อ่านข้อมูล','ล้าง'])
    def test_limit_and_expiry(self):
        for n in range(6): self.bot.handle(event(image=str(n+1)))
        self.assertEqual(len(self.bot.sessions['one']['images']),5)
        self.now+=1801; self.bot.cleanup(); self.assertFalse(self.bot.sessions)
    def test_group_ignored(self):
        e=event(image='1'); e['source']['type']='group'; self.bot.handle(e)
        self.assertFalse(self.sent); self.assertFalse(self.bot.sessions)
    def test_mixed_rejected(self):
        self.bot.reader=lambda _: {**record(),'status':'mixed'}
        self.bot.handle(event(image='1')); self.bot.handle(event('อ่านข้อมูล'))
        self.assertIsNone(self.bot.sessions['one']['record'])
    def test_api_request_and_null_fields(self):
        captured=[]
        def fake(url,token,data=None,**kw):
            if 'api-data.line.me' in url: return b'fake-image','image/jpeg'
            captured.append(data)
            return json.dumps({'status':'completed','output':[{'content':[{'type':'output_text','text':json.dumps(record())}]}]}).encode(),'application/json'
        with patch.dict(os.environ,LINE_CHANNEL_ACCESS_TOKEN='fake',OPENAI_API_KEY='fake'),patch.object(app,'api',fake):
            self.assertIsNone(app.extract(['123'])['vin'])
        self.assertFalse(captured[0]['store'])
        self.assertEqual(captured[0]['text']['format']['type'],'json_schema')
    def test_signature(self):
        body=b'{"events":[]}'
        sig=base64.b64encode(hmac.new(b'test',body,hashlib.sha256).digest()).decode()
        self.assertTrue(app.valid_signature(body,sig,'test'))
        self.assertFalse(app.valid_signature(body+b' ',sig,'test'))
    def test_http_and_dedup(self):
        os.environ['LINE_CHANNEL_SECRET']='test'
        app.SEEN.clear()
        server=app.ThreadingHTTPServer(('127.0.0.1',0),app.Handler)
        thread=threading.Thread(target=server.serve_forever,daemon=True); thread.start()
        def send(payload, valid=True):
            body=json.dumps(payload).encode()
            sig=base64.b64encode(hmac.new(b'test',body,hashlib.sha256).digest()).decode() if valid else 'wrong'
            req=urllib.request.Request('http://127.0.0.1:'+str(server.server_port)+'/webhook',data=body,headers={'X-Line-Signature':sig})
            return urllib.request.urlopen(req).status
        try:
            self.assertEqual(send({'events':[]}),200)
            with self.assertRaises(urllib.error.HTTPError) as ctx: send({'events':[]},False)
            self.assertEqual(ctx.exception.code,403)
            e=event('help'); e['webhookEventId']='test-event'
            before=app.JOBS.qsize(); send({'events':[e,e]}); send({'events':[e]})
            self.assertEqual(app.JOBS.qsize(),before+1)
        finally: server.shutdown(); server.server_close()

unittest.main()
