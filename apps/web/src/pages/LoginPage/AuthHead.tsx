import { Link } from "react-router-dom";
import { Brand, ModeBadge } from "@/components/ui/Brand";

/** Card header for login / register / forgot-password: brand, paper badge, page title. */
export function AuthHead({ title, sub }: { title: string; sub?: string }) {
  return (
    <header className="auth-head">
      <div className="auth-head-brand">
        <Link to="/login" aria-label="AUUTRADE 首页">
          <Brand />
        </Link>
        <ModeBadge />
      </div>
      <h1>{title}</h1>
      {sub ? <p className="td-note auth-sub">{sub}</p> : null}
    </header>
  );
}
