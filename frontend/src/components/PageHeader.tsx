import type { ReactNode } from "react";

export function PageHeader({
  index,
  eyebrow,
  title,
  description,
  action,
}: {
  index: string;
  eyebrow: string;
  title: string;
  description: string;
  action?: ReactNode;
}) {
  return (
    <header className="page-header">
      <div className="page-index">{index}</div>
      <div className="page-heading">
        <p className="eyebrow">{eyebrow}</p>
        <h1>{title}</h1>
        <p>{description}</p>
      </div>
      {action ? <div className="page-action">{action}</div> : null}
    </header>
  );
}

