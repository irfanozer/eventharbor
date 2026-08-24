import { useEffect, useRef } from "react";

import { shortId } from "../format";

export function ReplayConfirmation({
  open,
  deliveryId,
  busy,
  onCancel,
  onConfirm,
}: {
  open: boolean;
  deliveryId: string;
  busy: boolean;
  onCancel: () => void;
  onConfirm: () => void;
}) {
  const cancelRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    if (open) cancelRef.current?.focus();
  }, [open]);

  if (!open) return null;
  return (
    <div className="confirmation-backdrop" role="presentation" onMouseDown={(event) => {
      if (event.currentTarget === event.target && !busy) onCancel();
    }}>
      <section
        className="confirmation-panel"
        role="alertdialog"
        aria-modal="true"
        aria-labelledby="replay-title"
        aria-describedby="replay-description"
        onKeyDown={(event) => {
          if (event.key === "Escape" && !busy) onCancel();
        }}
      >
        <p className="eyebrow">Controlled recovery</p>
        <h2 id="replay-title">Approve a new delivery generation?</h2>
        <p id="replay-description">
          EventHarbor will keep generation 0 unchanged and create generation 1 from delivery {shortId(deliveryId)}.
        </p>
        <div className="confirmation-warning">
          <strong>This sends the webhook again.</strong>
          <span>The receiver is configured to succeed.</span>
        </div>
        <div className="confirmation-actions">
          <button ref={cancelRef} className="secondary-button" type="button" disabled={busy} onClick={onCancel}>Cancel</button>
          <button className="primary-button" type="button" disabled={busy} onClick={onConfirm}>
            {busy ? "Accepting replay…" : "Approve replay →"}
          </button>
        </div>
      </section>
    </div>
  );
}

