import * as React from "react";
import { cva, type VariantProps } from "class-variance-authority";
import { Loader2 } from "lucide-react";
import { cn } from "@/lib/utils";

// Token binding follows design-system/components/component_specs.md (button family).
// surface/text/border/radius/padding/focus-ring all route through --ds-* tokens.
const buttonVariants = cva(
  "inline-flex items-center justify-center gap-ds-8 rounded-md text-sm font-semibold " +
    "max-w-full min-w-0 whitespace-normal transition-colors duration-ds-base ease-ds-standard " +
    "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--ds-color-primary)] " +
    "focus-visible:ring-offset-2 focus-visible:ring-offset-[var(--ds-color-surface)] " +
    "disabled:pointer-events-none disabled:opacity-50 select-none",
  {
    variants: {
      variant: {
        primary:
          "bg-[var(--ds-color-primary)] text-[var(--ds-color-ink-inverse)] " +
          "border border-[var(--ds-color-primary)] hover:opacity-90",
        secondary:
          "bg-[var(--ds-color-surface)] text-[var(--ds-color-ink)] " +
          "border border-[var(--ds-color-border-strong)] hover:bg-[var(--ds-color-surface-muted)]",
        ghost:
          "bg-transparent text-[var(--ds-color-ink-muted)] border border-transparent " +
          "hover:bg-[var(--ds-color-surface-muted)] hover:text-[var(--ds-color-ink)]",
        danger:
          "bg-transparent text-[var(--ds-color-danger)] border border-[var(--ds-color-danger)] " +
          "hover:bg-[color-mix(in_srgb,var(--ds-color-danger)_12%,transparent)]",
        success:
          "bg-transparent text-[var(--ds-color-success)] border border-[var(--ds-color-success)] " +
          "hover:bg-[color-mix(in_srgb,var(--ds-color-success)_12%,transparent)]",
        link:
          "bg-transparent text-[var(--ds-color-link)] border border-transparent underline-offset-4 " +
          "hover:underline px-0",
      },
      size: {
        sm: "h-8 px-ds-12 text-xs",
        md: "h-10 px-ds-16",
        lg: "h-12 px-ds-24 text-md",
        icon: "h-10 w-10 px-0",
      },
    },
    defaultVariants: { variant: "primary", size: "md" },
  }
);

export interface ButtonProps
  extends React.ButtonHTMLAttributes<HTMLButtonElement>,
    VariantProps<typeof buttonVariants> {
  loading?: boolean;
}

export const Button = React.forwardRef<HTMLButtonElement, ButtonProps>(
  ({ className, variant, size, loading, children, disabled, ...props }, ref) => {
    return (
      <button
        ref={ref}
        role="button"
        aria-busy={loading || undefined}
        aria-disabled={disabled || undefined}
        disabled={disabled || loading}
        className={cn(buttonVariants({ variant, size }), className)}
        {...props}
      >
        {loading && <Loader2 aria-hidden className="h-4 w-4 animate-ds-spin" />}
        {children}
      </button>
    );
  }
);
Button.displayName = "Button";
