# Test Plan

## Objectives
- Validate the Flask authentication, patient management, diagnosis, and reporting workflows.
- Validate the React patient management UI and its interaction with firestor service helpers.
- Provide a repeatable automation path via pytest and Vitest.
- Establish CI execution in GitHub Actions and generate test artifacts.

## Scope
- Backend routes: signup, login, logout, patients, diagnose, reports.
- Frontend components: PatientForm, PatientList, Notification, Loading.
- QA artifacts: 120 documented test cases, Excel reports, markdown summaries, and CI workflow.

## Approach
1. Unit tests cover helper functions and component-level validation.
2. Integration tests cover route-to-route flows and frontend service wiring.
3. Manual and browser-based scenarios are documented for Firebase and deployment verification.

## Exit Criteria
- All automated tests pass in CI.
- Test reports and Excel workbooks are generated in the reports directory.
- The Vite frontend builds successfully.
