// API实体字段约定：来源{id,natural_key,data}、口径{id,version,data}、报告{id,status,snapshot,checksum,result,approved}、计划{next_at,enabled}。
// 勘误批次{id,name,status,old_policy,new_policy,activities,items[],targets[],completed_count,item_count}；明细{source_run_id,run_id,status,old_total,new_total,total_delta,rows[]}。
export const ENTITY_KEYS=['sources','policies','runs','schedules','deliveries','errata'];
