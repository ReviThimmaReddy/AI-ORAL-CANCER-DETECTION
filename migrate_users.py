import firebase_admin
from firebase_admin import credentials, firestore, auth

cred = credentials.Certificate("firebase-key.json")
firebase_admin.initialize_app(cred)
db = firestore.client()

def migrate_users():
    users_ref = db.collection("users")
    patients_ref = db.collection("patients")
    diagnoses_ref = db.collection("diagnoses")
    
    users = users_ref.stream()
    
    for user_doc in users:
        user_data = user_doc.to_dict()
        old_id = user_doc.id
        email = user_data.get("email")
        name = user_data.get("name", "Doctor")
        
        if "password_hash" not in user_data:
            print(f"User {email} already migrated or has no password_hash. Skipping.")
            continue
            
        print(f"Migrating user {email} (Old ID: {old_id})...")
        
        try:
            # Check if user already exists in Firebase Auth
            auth_user = auth.get_user_by_email(email)
            new_uid = auth_user.uid
            print(f"User {email} already in Firebase Auth with UID: {new_uid}")
        except firebase_admin.auth.UserNotFoundError:
            # Create user in Firebase Auth
            temp_password = "TempPassword123!"
            auth_user = auth.create_user(
                email=email,
                password=temp_password,
                display_name=name
            )
            new_uid = auth_user.uid
            print(f"Created Firebase Auth user {email} with UID: {new_uid}")
            
        # Update Patients collection
        patients = patients_ref.where("user_id", "==", old_id).stream()
        for patient in patients:
            patients_ref.document(patient.id).update({"user_id": new_uid})
            
        # Update Diagnoses collection
        diagnoses = diagnoses_ref.where("user_id", "==", old_id).stream()
        for diagnosis in diagnoses:
            diagnoses_ref.document(diagnosis.id).update({"user_id": new_uid})
            
        # Create new user document
        new_user_data = {
            "id": new_uid,
            "name": name,
            "email": email,
            "role": user_data.get("role", "Doctor"),
            "created_at": user_data.get("created_at", firestore.SERVER_TIMESTAMP)
        }
        users_ref.document(new_uid).set(new_user_data)
        
        # Delete old user document
        if old_id != new_uid:
            users_ref.document(old_id).delete()
            
        print(f"Migration complete for {email}.\n")

if __name__ == "__main__":
    print("Starting migration...")
    migrate_users()
    print("All done!")
