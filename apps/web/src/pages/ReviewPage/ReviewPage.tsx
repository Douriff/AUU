import { PostmortemPanel } from "@/components/market/PostmortemPanel";

export function ReviewPage() {
  return (
    <div className="shell-page review-page">
      <h1>复盘</h1>
      <p className="muted">已平仓纸面交易的出场分布和执行门槛。只读，不改策略参数，也不打开实盘。</p>
      <PostmortemPanel pollMs={4000} />
    </div>
  );
}
