import os
import httpx

# Load credentials from environment variables
SEMBLE_API_KEY = os.getenv("SEMBLE_API_KEY")
TEST_PATIENT_EMAIL = os.getenv("TEST_PATIENT_EMAIL")

async def run_diagnostics():
    """Tests various Semble API endpoints to find the correct one for patient search."""
    
    if not SEMBLE_API_KEY or not TEST_PATIENT_EMAIL:
        print("--- ERROR ---")
        print("Please ensure both SEMBLE_API_KEY and TEST_PATIENT_EMAIL are set in your environment variables.")
        return

    print("--- Starting Semble API Diagnostic Test ---")
    
    headers = {
        "Authorization": f"Bearer {SEMBLE_API_KEY}",
        "Accept": "application/json",
        "Content-Type": "application/json"
    }

    # --- FIX --- Added 'None' to the GET requests to ensure all tuples have 3 values.
    endpoints_to_test = [
        ("GET", f"https://api.semble.io/v1/patients?email={TEST_PATIENT_EMAIL}", None),
        ("GET", f"https://api.semble.io/v1/patients/search?email={TEST_PATIENT_EMAIL}", None),
        ("POST", "https://api.semble.io/v1/patients/search", {"email": TEST_PATIENT_EMAIL}),
        ("GET", f"https://api.semble.io/v1/users?email={TEST_PATIENT_EMAIL}", None),
    ]

    async with httpx.AsyncClient() as client:
        for i, (method, url, data) in enumerate(endpoints_to_test, 1):
            print(f"\n--- Test {i}: {method} {url} ---")
            try:
                if method == "GET":
                    response = await client.get(url, headers=headers)
                else: # POST
                    response = await client.post(url, headers=headers, json=data)
                
                response.raise_for_status()
                
                print(f"✅ SUCCESS! Status: {response.status_code}")
                print("Response Body:")
                print(response.json())
                print("\n==> THIS IS LIKELY THE CORRECT ENDPOINT AND METHOD! <==")
                
            except httpx.HTTPStatusError as e:
                print(f"❌ FAILED. Status: {e.response.status_code}")
                print(f"Response Body: {e.response.text}")
            except Exception as e:
                print(f"❌ FAILED. An unexpected error occurred: {e}")

    print("\n--- Diagnostic Test Complete ---")


if __name__ == "__main__":
    import asyncio
    asyncio.run(run_diagnostics())
