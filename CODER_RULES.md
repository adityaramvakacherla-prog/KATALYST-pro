# KATALYST CODER RULES
# Read this before every single task. These rules are absolute and non-negotiable.
# The goal is always: code so polished and professional it looks like it was built by a senior team at a top tech company.

---

## MINDSET
- You are not writing a demo or a prototype. You are writing production software.
- Every file you produce should look like it belongs in a real, shipped product.
- If you wouldn't be proud to show this code to a senior engineer, rewrite it.
- Polish matters. Spacing, alignment, naming, comments — all of it.

---

## CODE QUALITY

### Completeness
- Implement EVERY feature described. Read the task description word by word — nothing skipped.
- Every function body must be fully implemented. Zero placeholders.
- No `pass`, no `# TODO`, no `raise NotImplementedError`, no `return None` where real logic is needed.
- No commented-out code. No dead code. No debug prints left in.

### Error Handling
- Every I/O operation (file, network, database, parse) must have try/except.
- Never swallow exceptions silently. Always log or surface the error meaningfully.
- Fail gracefully — show the user a friendly error message, never a raw traceback.
- Validate inputs before processing them.

### Structure
- One function = one job. If a function does two things, split it.
- Functions under 25 lines. If longer, extract helpers with clear names.
- Related functions grouped together with a short comment block header.
- Constants at the top of the file, never buried inside functions.
- Imports grouped: stdlib first, then third-party, then local. One blank line between groups.

### Naming
- Variables, functions, and classes named for what they ARE, not what they DO generically.
- Bad: `data`, `result`, `temp`, `val`, `x`. Good: `user_score`, `filtered_tasks`, `canvas_element`.
- Boolean variables start with `is_`, `has_`, `can_`, `should_`.
- Constants in UPPER_SNAKE_CASE. Classes in PascalCase. Functions and variables in snake_case (Python) or camelCase (JS).

### Comments
- Every function has a one-line docstring/comment explaining what it does and what it returns.
- Complex logic gets an inline comment explaining WHY, not just WHAT.
- No redundant comments (`# increment i` above `i += 1`).

---

## UI & VISUAL QUALITY (HTML/JS/CSS)

### Theme — Dark, Modern, Professional
- Background: `#0e1117` (deep dark)
- Surface/cards: `#151b26`
- Border: `#252f45`
- Primary text: `#e2e8f0`
- Secondary text: `#8892a4`
- Accent/primary: `#7c6af7` (purple)
- Accent hover: `#9b8dff`
- Success: `#3dd68c`
- Error: `#f05252`
- Warning: `#f5a623`
- Font: `'Inter', 'Segoe UI', system-ui, sans-serif` for UI. `'JetBrains Mono', 'Fira Code', monospace` for code.

### Layout
- Use CSS Grid or Flexbox — never floats or tables for layout.
- Consistent spacing: 8px base unit. Use multiples (8, 16, 24, 32, 48).
- Cards/panels: `border-radius: 12px`, subtle border, slight shadow.
- Full viewport height layouts: `min-height: 100vh`.
- Responsive by default: nothing should overflow or break on normal screen sizes.

### Interactivity
- Every button has a hover state (colour shift + cursor: pointer).
- Every interactive element has a visible focus state.
- Smooth transitions on hover/focus: `transition: all 0.15s ease`.
- Buttons have padding: minimum `10px 20px`. Never tiny click targets.
- Disabled states are visually distinct: `opacity: 0.45; cursor: not-allowed`.
- Loading states shown when async operations run.

### Typography
- Clear hierarchy: large title → section heading → body → caption.
- Line height: 1.6 for body text. Never cramped.
- Letter spacing on uppercase labels: `letter-spacing: 1.5px`.
- Never use default browser fonts — always specify a font stack.

### Games specifically
- Canvas fills the available space. Centred. With a subtle border/glow.
- Score displayed prominently — large, visible, never hidden in a corner.
- Game over screen with final score, styled, with restart button.
- Smooth animation — use `requestAnimationFrame`, never `setInterval` for game loops.
- Keyboard controls work immediately — no click-to-focus needed.
- Mobile: touch controls if space allows, otherwise note keyboard required.

---

## PYTHON QUALITY

- Use f-strings for string formatting, never `.format()` or `%`.
- Type hints on all function signatures when the types are clear.
- Use `pathlib.Path` for file paths, not string concatenation.
- `if __name__ == "__main__":` guard on all executable scripts.
- Logging with the `logging` module for anything beyond a simple script, not bare `print()`.
- Use context managers (`with` statements) for file and resource handling.
- List/dict comprehensions where they improve readability — but not nested 3 levels deep.

---

## FINAL CHECK BEFORE SUBMITTING
Ask yourself these questions:
1. Does this implement EVERYTHING in the task description?
2. Would this crash on first run? On bad input?
3. Does the UI look polished — spacing, colours, hover states, typography?
4. Are function names and variable names specific and meaningful?
5. Is there any placeholder, TODO, or dead code left?
6. Would a senior engineer be proud of this output?

If any answer is NO — fix it before returning the code.
