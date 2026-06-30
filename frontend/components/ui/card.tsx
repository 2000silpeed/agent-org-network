import * as React from "react";
import { cn } from "@/lib/utils";

// data-display surface. elevation bias = raised, restrained depth, thin dividers.
export function Card({
  className,
  elevated,
  ...props
}: React.HTMLAttributes<HTMLDivElement> & { elevated?: boolean }) {
  return (
    <div
      className={cn(
        "rounded-lg border border-[var(--ds-color-border)]",
        elevated
          ? "bg-[var(--ds-color-surface-elevated)]"
          : "bg-[var(--ds-color-surface)]",
        className
      )}
      {...props}
    />
  );
}

export function CardHeader({
  className,
  ...props
}: React.HTMLAttributes<HTMLDivElement>) {
  return (
    <div
      className={cn(
        "flex flex-col gap-ds-4 border-b border-[var(--ds-color-border)] px-ds-16 py-ds-12",
        className
      )}
      {...props}
    />
  );
}

export function CardTitle({
  className,
  ...props
}: React.HTMLAttributes<HTMLHeadingElement>) {
  return (
    <h3
      className={cn(
        "font-heading text-md font-semibold text-[var(--ds-color-ink)]",
        className
      )}
      {...props}
    />
  );
}

export function CardBody({
  className,
  ...props
}: React.HTMLAttributes<HTMLDivElement>) {
  return <div className={cn("px-ds-16 py-ds-16", className)} {...props} />;
}

export function CardFooter({
  className,
  ...props
}: React.HTMLAttributes<HTMLDivElement>) {
  return (
    <div
      className={cn(
        "flex flex-wrap items-center gap-ds-8 border-t border-[var(--ds-color-border)] px-ds-16 py-ds-12",
        className
      )}
      {...props}
    />
  );
}
