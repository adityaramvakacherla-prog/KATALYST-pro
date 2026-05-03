from error_handler import run_and_check, classify_error
from api_handler import smart_ask
from logger import log
import time

MAX_RETRIES = 3

def code_with_retry(task_description, expected_output, previous_code=None):
    error_history = []
    current_code = previous_code

    for attempt in range(1, MAX_RETRIES + 1):
        log(f"Coding attempt {attempt}/{MAX_RETRIES}...")

        if attempt == 1:
            prompt = f"""
Task: {task_description}
Expected result: {expected_output}
Write complete working Python code only. No explanations. No markdown.
"""
        else:
            prompt = f"""
Task: {task_description}
Expected result: {expected_output}
Previous attempt failed with this error: {error_history[-1]}
Previous code that failed:
{current_code}
Fix the error and write the complete corrected code only. No explanations. No markdown.
"""

        error_type = classify_error(error_history[-1]) if error_history else "none"

        if error_type in ["logic", "timeout", "unknown"] and attempt > 1:
            current_code = smart_ask(prompt, mode="plan")
        else:
            current_code = smart_ask(prompt, mode="code")

        if not current_code:
            log("API returned nothing — skipping attempt", "WARNING")
            continue

        success, result = run_and_check(current_code, expected_output)

        if success:
            log(f"Success on attempt {attempt}")
            return True, current_code, error_history
        else:
            log(f"Attempt {attempt} failed: {result[:100]}", "WARNING")
            error_history.append(result)
            time.sleep(2)

    log("All retries failed — escalating to stronger model", "WARNING")
    final_code = escalate_to_coder_pro(task_description, expected_output, error_history)

    if final_code:
        return True, final_code, error_history
    else:
        return False, current_code, error_history

def escalate_to_coder_pro(task, expected_output, error_history):
    """
    Coder Pro — same model but with expert level prompt.
    Called only after 3 failed attempts.
    """
    log("Escalating to CODER PRO — expert prompt mode...", "WARNING")

    from api_handler import groq_client, strip_markdown, write_live

    prompt = f"""You are a senior Python developer with 15 years of experience.
A junior developer attempted this task 3 times and failed every time.
Your job is to analyze the failures and write a perfect solution.

═══ TASK ═══
{task}

═══ EXPECTED OUTPUT ═══
{expected_output}

═══ FAILURE ANALYSIS ═══
Attempt 1 failed with: {error_history[0] if len(error_history) > 0 else 'none'}
Attempt 2 failed with: {error_history[1] if len(error_history) > 1 else 'none'}
Attempt 3 failed with: {error_history[2] if len(error_history) > 2 else 'none'}

═══ YOUR INSTRUCTIONS ═══
1. Study each error carefully — understand WHY it failed
2. Do NOT repeat any approach that already failed
3. Write a completely fresh implementation from scratch
4. Every function must be fully implemented — no placeholders
5. Every potential error must be caught with try/except
6. Test your logic mentally before writing it

═══ OUTPUT FORMAT ═══
Return Python code ONLY.
No explanations. No markdown. No code fences.
Start directly with import statements or def/class.
"""

    try:
        stream = groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[
                {
                    "role": "system",
                    "content": "You are an expert Python developer. Return only clean working Python code. Never use markdown fences. Never add explanations. Start directly with code."
                },
                {
                    "role": "user",
                    "content": prompt
                }
            ],
            max_tokens=8000,
            temperature=0.1,
            stream=True
        )
        full_response = ""
        write_live("🧠 CODER PRO ANALYZING FAILURES...\n")
        for chunk in stream:
            delta = chunk.choices[0].delta.content
            if delta:
                full_response += delta
                write_live("🧠 CODER PRO:\n" + full_response + "▋")

        write_live(full_response)
        result = strip_markdown(full_response)

        if result:
            log(f"Coder Pro succeeded — {len(result)} chars", "SUCCESS")
            return result
        else:
            log("Coder Pro returned empty", "WARNING")
            from api_handler import ask_groq
            return ask_groq(prompt)

    except Exception as e:
        log(f"Coder Pro failed: {str(e)[:100]}", "WARNING")
        from api_handler import ask_groq
        return ask_groq(prompt)
