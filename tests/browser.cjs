// Optional rendered-report smoke test. npm install --no-save playwright
const {chromium}=require('playwright');
const assert=require('node:assert/strict');
const path=require('node:path');
(async()=>{
 const browser=await chromium.launch({headless:true,executablePath:process.env.CHROMIUM_EXECUTABLE||undefined,args:['--disable-gpu']});
 try{
  const page=await browser.newPage({viewport:{width:1280,height:900}});
  const errors=[];page.on('pageerror',e=>errors.push(e.message));
  await page.goto('file://'+path.resolve('reports/sample-analysis.html'));
  assert.equal(await page.locator('table tr').count(),7);
  assert.match(await page.locator('body').innerText(),/Missing parent/);
  assert.match(await page.locator('body').innerText(),/ERROR SLOW/);
  assert.equal(await page.locator('.bar').count(),6);
  await page.screenshot({path:'/tmp/tracelens-preview.png'});
  await page.setViewportSize({width:390,height:844});
  assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
  assert.deepEqual(errors,[]);
  console.log('Browser passed: six timeline bars, tree rows, error/slow markers, warnings, mobile layout; no JS errors.');
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exit(1)});
