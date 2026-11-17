import {heading} from '../components/common/header.js';import {table} from '../components/common/table.js';import {esc,time,fmt} from '../utils/format.js';import {ERRATA_STATUSES} from '../constants/enums.js';
export function refreshActivityChoices(policies){const form=document.querySelector('#errata-form');if(!form)return;const oldId=Number(form.querySelector('[name=old_policy_id]').value),newId=Number(form.querySelector('[name=new_policy_id]').value);const box=form.querySelector('.activities');const checked=[...box.querySelectorAll('input:checked')].map(x=>x.value);box.innerHTML=activityChoices(policies,oldId,newId,checked)}
export const errataStatus=s=>'<span class="status '+esc(s)+'">'+(ERRATA_STATUSES[s]||esc(s))+'</span>';

const activityChoices=(policies,oldId,newId,selected)=>{const old=(policies.find(p=>p.id===oldId)||{}).data,now=(policies.find(p=>p.id===newId)||{}).data;const names=[...new Set([...Object.keys(old?.factors||{}),...Object.keys(now?.factors||{})])];return names.map(n=>`<label class="chk"><input type="checkbox" name="activity" value="${esc(n)}" ${selected.includes(n)?'checked':''}> ${esc(n)}</label>`).join('')};

export function errata(c,d,selectedErrata){
 const full=d.errata.find(x=>x.id===selectedErrata)||d.errata[0];
 const seedOld=d.policies[0],seedNew=d.policies.at(-1);
 return heading('ERRATA REGISTER / 05','因子勘误成组重述','选择旧、新因子版本及受影响活动：只从原报告冻结底稿重算相关报告，保留原报告与已发交付；整组算完并复核后才能原子切换所关联的交付计划。')
 +`<form id="errata-form"><div class="form-grid"><label>勘误批次名称<input name="name" value="因子勘误·电力" required minlength="2"></label><label>旧因子版本<select name="old_policy_id">${d.policies.map(p=>`<option value="${p.id}" ${p.id===seedOld?.id?'selected':''}>${esc(p.version)}</option>`).join('')}</select></label><label>新因子版本<select name="new_policy_id">${d.policies.map(p=>`<option value="${p.id}" ${p.id===seedNew?.id?'selected':''}>${esc(p.version)}</option>`).join('')}</select></label></div><h3>受影响活动</h3><div class="activities">${activityChoices(d.policies,seedOld?.id,seedNew?.id,[])}</div><p class="hint">仅包含所选活动的旧因子已复核报告进入整组；新版本中其他活动因子也必须与旧版本一致，否则请先拆分版本。</p><button ${d.policies.length<2?'disabled':''}>发起成组勘误</button>${d.policies.length<2?'<p class="hint">请先在“口径版本”页登记包含修正因子的新版本。</p>':''}</form>`
 +`<h2>勘误批次</h2><div class="run-layout"><aside class="run-list">${d.errata.map(b=>`<button class="select-errata" data-id="${b.id}">${esc(b.name)} ${errataStatus(b.status)}<small>${b.completed_count}/${b.item_count} 报告 · ${b.targets.length} 计划</small></button>`).join('')||'<p class="empty">尚无勘误批次。</p>'}</aside><div>${full?batchDetail(full):'<p class="empty">选择一个勘误批次查看重述报告与差额。</p>'}</div></div>`;
}

const num=v=>v===null||v===undefined?'—':fmt(v);
function batchDetail(b){
 const canSwitch=b.status==='reviewed';
 const actions=[];
 if(b.status==='failed')actions.push('<button class="errata-retry" data-id="'+b.id+'">重试失败重述</button>');
 if(b.status==='review'||b.status==='reviewed')actions.push(`<form id="errata-review" data-id="${b.id}" class="inline-form"><label>负责人复核说明<input name="note" minlength="4" value="${esc(b.review||'')}" ${b.status==='reviewed'?'disabled':''} required></label><button ${b.status==='reviewed'?'disabled':''}>整组复核</button></form>`);
 if(b.status!=='switched')actions.push(`<button class="errata-switch" data-id="${b.id}" ${canSwitch?'':'disabled'}>原子切换交付计划</button>`);
 const rows=b.items.flatMap(it=>it.rows.map(r=>({报告:it.source_name,月份:r.月份,部门:r.部门,活动:r.活动,'原排放kgCO2e':r['原排放kgCO2e'],'重述排放kgCO2e':r['重述排放kgCO2e'],'差额kgCO2e':r['差额kgCO2e']})));
 return `<section class="report"><div class="report-top"><h2>${esc(b.name)}</h2>${errataStatus(b.status)}</div><p>${esc(b.old_policy)} → ${esc(b.new_policy)} · 活动 ${esc(b.activities.join('、'))} · 发起人 ${esc(b.created_by)} · ${time(b.created)}</p>${b.error?'<p class="error">'+esc(b.error)+'</p>':''}<p>整组报告：${b.completed_count}/${b.item_count} 完成${b.failed_count?'，'+b.failed_count+' 失败':''}；关联交付计划 ${b.targets.length} 个。${b.status!=='switched'?'旧组继续生效。':'新组已生效。'}</p>${b.review?'<p class="method">复核：'+esc(b.review)+'（'+esc(b.reviewed_by)+' · '+time(b.reviewed_at)+'）</p>':''}
 <h3>重述报告与差额</h3><div class="table-wrap"><table><thead><tr><th>编号</th><th>原报告</th><th>重述报告</th><th>原合计</th><th>重述合计</th><th>差额</th><th>状态</th></tr></thead><tbody>${b.items.map(it=>`<tr><td>${it.id}</td><td>${it.source_run_id} ${esc(it.source_name||'')}</td><td>${it.run_id??'-'}${it.status==='completed'?` <a class="errata-csv" data-batch="${b.id}" data-item="${it.id}" href="#">下载</a>`:''}</td><td>${num(it.old_total)}</td><td>${num(it.new_total)}</td><td>${num(it.total_delta)}</td><td>${esc(it.status)}${it.error?'<br><small class="error">'+esc(it.error)+'</small>':''}</td></tr>`).join('')}</tbody></table></div>
 <h3>逐行差额</h3>${table(rows)}
 <h3>关联交付计划</h3>${table(b.targets.map(t=>({编号:t.id,计划:t.schedule_name,原报告:t.old_run_id,新报告:t.new_run_id??'待生成',原代次:t.expected_generation,当前代次:t.current_generation,已切换:t.switched?'是':'否'})))}
 <div class="report-actions">${actions.join('')}</div></section>`;
}
