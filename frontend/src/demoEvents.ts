import type {
  Delivery,
  DeliveryAttempt,
  DemoEventDataValue,
  DemoEventPayload,
  DemoEventTypeId,
} from "./types";

export const RECEIVER_LAB_BASE_URL = "http://receiver-lab:8100/webhooks";

export interface DemoEventField {
  key: string;
  label: string;
  kind: "text" | "money" | "integer";
  help: string;
  initialValue: (suffix: string) => string;
}

export interface DemoEventDefinition {
  type: DemoEventTypeId;
  label: string;
  description: string;
  referenceField: string;
  invalidField: string;
  receiverRequirement: string;
  destinationName: string;
  destinationUrl: string;
  destinationPurpose: string;
  fields: readonly DemoEventField[];
}

export const DEMO_EVENT_TYPES: readonly DemoEventDefinition[] = [
  {
    type: "order.paid",
    label: "Order paid",
    description: "Send a paid order to fulfillment.",
    referenceField: "order_id",
    invalidField: "customer_id",
    receiverRequirement: "data.customer_id must be a non-empty string.",
    destinationName: "Orders receiver · Receiver Lab",
    destinationUrl: `${RECEIVER_LAB_BASE_URL}/orders`,
    destinationPurpose: "The fulfillment route inside the local Receiver Lab service.",
    fields: [
      { key: "order_id", label: "Order ID", kind: "text", help: "Business identifier", initialValue: (suffix) => `ORDER-${suffix}` },
      { key: "customer_id", label: "Customer ID", kind: "text", help: "Required by the orders receiver", initialValue: (suffix) => `CUS-${suffix.slice(0, 6)}` },
      { key: "amount_cents", label: "Amount", kind: "money", help: "USD", initialValue: () => "129.00" },
      { key: "currency", label: "Currency", kind: "text", help: "ISO 4217 code", initialValue: () => "USD" },
    ],
  },
  {
    type: "shipment.dispatched",
    label: "Shipment dispatched",
    description: "Notify a shipping consumer that a parcel left the warehouse.",
    referenceField: "shipment_id",
    invalidField: "tracking_number",
    receiverRequirement: "data.tracking_number must be a non-empty string.",
    destinationName: "Shipping receiver · Receiver Lab",
    destinationUrl: `${RECEIVER_LAB_BASE_URL}/shipping`,
    destinationPurpose: "The shipping route inside the local Receiver Lab service.",
    fields: [
      { key: "shipment_id", label: "Shipment ID", kind: "text", help: "Business identifier", initialValue: (suffix) => `SHIP-${suffix}` },
      { key: "order_id", label: "Order ID", kind: "text", help: "Related order", initialValue: (suffix) => `ORDER-${suffix}` },
      { key: "carrier", label: "Carrier", kind: "text", help: "Delivery provider", initialValue: () => "DHL" },
      { key: "tracking_number", label: "Tracking number", kind: "text", help: "Required by the shipping receiver", initialValue: (suffix) => `DHL-${suffix}US` },
    ],
  },
  {
    type: "inventory.threshold_reached",
    label: "Inventory threshold reached",
    description: "Ask an inventory consumer to begin replenishment.",
    referenceField: "sku",
    invalidField: "warehouse_id",
    receiverRequirement: "data.warehouse_id must be a non-empty string.",
    destinationName: "Inventory receiver · Receiver Lab",
    destinationUrl: `${RECEIVER_LAB_BASE_URL}/inventory`,
    destinationPurpose: "The inventory route inside the local Receiver Lab service.",
    fields: [
      { key: "sku", label: "SKU", kind: "text", help: "Stock keeping unit", initialValue: (suffix) => `SKU-${suffix}` },
      { key: "warehouse_id", label: "Warehouse ID", kind: "text", help: "Required by the inventory receiver", initialValue: () => "WH-NYC-01" },
      { key: "quantity_remaining", label: "Quantity remaining", kind: "integer", help: "Current stock", initialValue: () => "4" },
      { key: "reorder_threshold", label: "Reorder threshold", kind: "integer", help: "Restock trigger", initialValue: () => "10" },
    ],
  },
] as const;

export const DEFAULT_DEMO_EVENT_TYPE: DemoEventTypeId = "order.paid";

export function isDemoEventTypeId(value: unknown): value is DemoEventTypeId {
  return typeof value === "string" && DEMO_EVENT_TYPES.some((event) => event.type === value);
}

export function demoEventDefinition(type: unknown): DemoEventDefinition {
  return DEMO_EVENT_TYPES.find((event) => event.type === type) ?? DEMO_EVENT_TYPES[0]!;
}

export function initialDemoEventValues(type: DemoEventTypeId): Record<string, string> {
  const suffix = crypto.randomUUID().slice(0, 8).toUpperCase();
  const definition = demoEventDefinition(type);
  return Object.fromEntries(
    definition.fields.map((field) => [field.key, field.initialValue(suffix)]),
  );
}

export function demoEventPayload(
  type: DemoEventTypeId,
  values: Record<string, string>,
): DemoEventPayload {
  const definition = demoEventDefinition(type);
  const data: Record<string, DemoEventDataValue> = {};
  for (const field of definition.fields) {
    const value = values[field.key] ?? "";
    if (field.kind === "money") data[field.key] = Math.round(Number.parseFloat(value) * 100);
    else if (field.kind === "integer") data[field.key] = Number.parseInt(value, 10);
    else data[field.key] = value.trim();
  }
  return { type, data };
}

export function demoEventReference(payload: DemoEventPayload): string {
  const definition = demoEventDefinition(payload.type);
  const value = payload.data[definition.referenceField];
  return typeof value === "string" && value ? value : "Event accepted";
}

export function isCanonicalDemoEndpoint(url: string): boolean {
  return DEMO_EVENT_TYPES.some((event) => event.destinationUrl === url);
}

export function isExpectedSchemaRejection(
  eventType: unknown,
  delivery: Delivery | null | undefined,
  attempts: DeliveryAttempt[],
): boolean {
  if (
    !isDemoEventTypeId(eventType) ||
    !delivery ||
    delivery.status !== "dead_lettered" ||
    delivery.attempt_count !== 1 ||
    attempts.length !== 1
  ) return false;

  const attempt = attempts[0];
  if (
    attempt?.status !== "completed" ||
    attempt.attempt_number !== 1 ||
    attempt.http_status_code !== 400 ||
    attempt.disposition !== "terminal_failure"
  ) return false;

  try {
    const response = JSON.parse(attempt.response_body_excerpt ?? "") as Record<string, unknown>;
    return response.code === `missing_${demoEventDefinition(eventType).invalidField}`;
  } catch {
    return false;
  }
}
