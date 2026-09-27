import os
import uuid
from datetime import datetime
from functools import wraps

import firebase_admin
import numpy as np
from flask import (
    abort,
    Flask,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    send_from_directory,
    session,
    url_for,
)
import requests
from PIL import Image
from firebase_admin import credentials, firestore, auth as firebase_auth
from werkzeug.utils import secure_filename


BASE_DIR = os.path.abspath(os.path.dirname(__file__))
UPLOAD_FOLDER = os.path.join(BASE_DIR, "uploads")
MODEL_PATH = os.path.join(BASE_DIR, "oral_model.tflite")
ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "webp"}
CLASS_LABELS = {0: "Normal", 1: "OSCC"}

os.makedirs(UPLOAD_FOLDER, exist_ok=True)

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "change-this-secret-key-in-production")
app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER
app.config["MAX_CONTENT_LENGTH"] = 8 * 1024 * 1024
FIREBASE_API_KEY = "AIzaSyCqGkY4Vv-q9eBRLgMdwKYlIVfQYk4DPnE"

interpreter = None
input_details = None
output_details = None

db = None
users_ref = None
patients_ref = None
diagnoses_ref = None


class FallbackDocument:
    def __init__(self, collection, doc_id, data=None, exists=True):
        self.collection = collection
        self.id = doc_id
        self._data = data.copy() if data else {}
        self.exists = exists

    def to_dict(self):
        return self._data.copy()


class FallbackDocumentRef:
    def __init__(self, collection, doc_id):
        self.collection = collection
        self.id = doc_id

    def set(self, data, merge=False, **kwargs):
        existing = self.collection._items.get(self.id)
        if merge and existing:
            merged = dict(existing)
            merged.update(data)
            data = merged
        else:
            data = dict(data)
        data.setdefault("id", self.id)
        self.collection._items[self.id] = data
        return None

    def get(self, **kwargs):
        data = self.collection._items.get(self.id)
        if data is None:
            return FallbackDocument(self.collection, self.id, exists=False)
        return FallbackDocument(self.collection, self.id, data=data, exists=True)

    def delete(self):
        self.collection._items.pop(self.id, None)


class FallbackQuery:
    def __init__(self, collection, field=None, operator=None, value=None, limit=None):
        self.collection = collection
        self.field = field
        self.operator = operator
        self.value = value
        self.limit_value = limit

    def where(self, field, operator, value):
        return FallbackQuery(self.collection, field=field, operator=operator, value=value, limit=self.limit_value)

    def limit(self, count):
        return FallbackQuery(self.collection, field=self.field, operator=self.operator, value=self.value, limit=count)

    def stream(self, **kwargs):
        docs = []
        for doc_id, data in self.collection._items.items():
            if self.field is None:
                match = True
            else:
                actual = data.get(self.field)
                if self.operator == "==":
                    match = actual == self.value
                else:
                    match = True
            if match:
                docs.append(FallbackDocument(self.collection, doc_id, data=data, exists=True))
        if self.limit_value is not None:
            docs = docs[: self.limit_value]
        return docs


class FallbackCollection:
    def __init__(self, name):
        self.name = name
        self._items = {}

    def document(self, doc_id=None):
        if doc_id is None:
            doc_id = str(len(self._items) + 1)
            while doc_id in self._items:
                doc_id = str(int(doc_id) + 1)
        return FallbackDocumentRef(self, str(doc_id))

    def where(self, field, operator, value):
        return FallbackQuery(self, field=field, operator=operator, value=value)


def _reset_fallback_state():
    global users_ref, patients_ref, diagnoses_ref
    users_ref = FallbackCollection("users")
    patients_ref = FallbackCollection("patients")
    diagnoses_ref = FallbackCollection("diagnosis_records")


try:
    if not firebase_admin._apps:
        cred = credentials.Certificate("firebase-key.json")
        firebase_admin.initialize_app(cred)
    if firebase_admin._apps:
        db = firestore.client()
        users_ref = db.collection("users")
        patients_ref = db.collection("patients")
        diagnoses_ref = db.collection("diagnosis_records")
except Exception as exc:
    app.logger.warning("Firebase initialization failed: %s", exc)
    _reset_fallback_state()


if users_ref is None or patients_ref is None or diagnoses_ref is None:
    _reset_fallback_state()


def load_model():
    global interpreter, input_details, output_details
    if interpreter is not None:
        return
    if not os.path.exists(MODEL_PATH):
        app.logger.warning("oral_model.tflite was not found at %s", MODEL_PATH)
        return
    import tensorflow as tf

    interpreter = tf.lite.Interpreter(model_path=MODEL_PATH)
    interpreter.allocate_tensors()
    input_details = interpreter.get_input_details()
    output_details = interpreter.get_output_details()


def login_required(view):
    @wraps(view)
    def wrapped_view(*args, **kwargs):
        if "user_id" not in session:
            flash("Please log in to continue.", "warning")
            return redirect(url_for("login"))
        return view(*args, **kwargs)

    return wrapped_view


def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def _serialize_doc(doc):
    data = doc.to_dict() or {}
    data["id"] = doc.id
    if "created_at" in data:
        if hasattr(data["created_at"], "to_datetime"):
            data["created_at"] = data["created_at"].to_datetime().isoformat()
        elif hasattr(data["created_at"], "isoformat"):
            data["created_at"] = data["created_at"].isoformat()
        else:
            data["created_at"] = str(data["created_at"])
    return data


def _created_at_value(data):
    created_at = data.get("created_at")
    if hasattr(created_at, "to_datetime"):
        return created_at.to_datetime()
    return created_at or datetime.min


def _get_user_by_email(email):
    docs = users_ref.where("email", "==", email).limit(1).stream(timeout=5.0)
    for doc in docs:
        return _serialize_doc(doc)
    return None


def _get_user_by_id(user_id):
    doc = users_ref.document(user_id).get(timeout=5.0)
    if not doc.exists:
        return None
    return _serialize_doc(doc)


def _get_patient_by_id(patient_id):
    doc = patients_ref.document(str(patient_id)).get(timeout=5.0)
    if not doc.exists:
        return None
    return _serialize_doc(doc)


def _get_diagnosis_by_id(diagnosis_id):
    doc = diagnoses_ref.document(str(diagnosis_id)).get(timeout=5.0)
    if not doc.exists:
        return None
    return _serialize_doc(doc)


def current_user():
    if "user_id" not in session:
        return None
    return _get_user_by_id(session["user_id"])


@app.context_processor
def inject_user():
    return {"current_user": current_user()}


def is_valid_microscopic_image(image_path):
    try:
        image = Image.open(image_path)
        if image.width < 50 or image.height < 50:
            return False
            
        image = image.convert("RGB")
        image.thumbnail((224, 224))
        img_array = np.array(image, dtype=np.int32)
        
        if np.std(img_array) < 5:
            return False
            
        r = img_array[:,:,0]
        g = img_array[:,:,1]
        b = img_array[:,:,2]
        
        mean_r = np.mean(r)
        mean_g = np.mean(g)
        mean_b = np.mean(b)
        
        if mean_g > mean_r and mean_g > mean_b:
            return False
            
        pink_pixels = np.sum((r > g) & (r > 50))
        total_pixels = img_array.shape[0] * img_array.shape[1]
        
        if (pink_pixels / total_pixels) < 0.05:
            return False
            
        return True
    except Exception:
        return False


def preprocess_image(image_path):
    try:
        if not is_valid_microscopic_image(image_path):
            raise ValueError("Please upload a valid microscopic oral cytology image.")
            
        image = Image.open(image_path).convert("RGB")
        image = image.resize((224, 224))
        image_array = np.asarray(image, dtype=np.float32) / 255.0
        return np.expand_dims(image_array, axis=0)
    except ValueError:
        raise
    except Exception:
        raise ValueError("Please upload a valid microscopic oral cytology image.")


def predict_image(image_path):
    load_model()
    if interpreter is None:
        raise RuntimeError("Model file oral_model.tflite is missing from the project root.")

    image_array = preprocess_image(image_path)
    expected_dtype = input_details[0]["dtype"]
    if expected_dtype != np.float32:
        image_array = image_array.astype(expected_dtype)

    interpreter.set_tensor(input_details[0]["index"], image_array)
    interpreter.invoke()
    predictions = interpreter.get_tensor(output_details[0]["index"])
    scores = np.asarray(predictions[0], dtype=np.float32)
    class_index = int(np.argmax(scores))
    confidence = float(scores[class_index])

    if confidence <= 1.0:
        confidence *= 100.0

    if confidence < 90.0:
        raise ValueError("Please upload a valid microscopic oral cytology image.")

    return CLASS_LABELS.get(class_index, "Unknown"), round(confidence, 2)


@app.route("/")
def index():
    if "user_id" in session:
        return redirect(url_for("dashboard"))
    return redirect(url_for("login"))


@app.route("/signup", methods=["GET", "POST"])
def signup():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")

        if not name or not email or not password:
            flash("All fields are required.", "danger")
            return render_template("signup.html")

        if not email or ("@" not in email) or (not email.endswith("@gmail.com") and not email.endswith("@example.com")):
            flash("Please enter a valid Gmail address.", "danger")
            return render_template("signup.html")

        existing = _get_user_by_email(email)
        if existing:
            flash("An account with this Gmail already exists.", "danger")
            return render_template("signup.html")

        try:
            # Create user in Firebase Auth
            user_record = firebase_auth.create_user(email=email, password=password)
            user_id = user_record.uid
            
            # Save user details to Firestore
            users_ref.document(user_id).set(
                {
                    "uid": user_id,
                    "id": user_id,
                    "full_name": name,
                    "name": name,
                    "email": email,
                    "role": "Doctor",
                    "created_at": firestore.SERVER_TIMESTAMP
                }
            )
            flash("Account created. Please log in.", "success")
            return redirect(url_for("login"))
        except Exception as e:
            flash(f"Error creating account: {str(e)}", "danger")
            return render_template("signup.html")

    return render_template("signup.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")

        try:
            # Verify via Firebase REST API
            res = requests.post(
                f"https://identitytoolkit.googleapis.com/v1/accounts:signInWithPassword?key={FIREBASE_API_KEY}",
                json={"email": email, "password": password, "returnSecureToken": True}
            )
            if res.status_code == 200:
                user_id = res.json().get("localId")
                user_doc = users_ref.document(user_id).get()
                
                if user_doc.exists:
                    user_data = user_doc.to_dict()
                    session.clear()
                    session["user_id"] = user_id
                    session["user_name"] = user_data.get("full_name", user_data.get("name", "Doctor"))
                    flash("Welcome back.", "success")
                    return redirect(url_for("dashboard"))
                else:
                    flash("User profile not found in database.", "danger")
            else:
                flash("Invalid email or password.", "danger")
        except Exception as e:
            flash(f"Login failed: {str(e)}", "danger")

        return render_template("login.html")

    return render_template("login.html")


@app.route("/forgot-password", methods=["POST"])
def forgot_password():
    email = request.form.get("reset_email", "").strip().lower()
    if not email:
        flash("Please enter your email address.", "danger")
        return redirect(url_for("login"))
        
    try:
        res = requests.post(
            f"https://identitytoolkit.googleapis.com/v1/accounts:sendOobCode?key={FIREBASE_API_KEY}",
            json={"requestType": "PASSWORD_RESET", "email": email}
        )
        
        if res.status_code == 200:
            flash("Password reset email sent. Please check your inbox.", "success")
        else:
            error_data = res.json()
            error_message = error_data.get("error", {}).get("message", "")
            
            if error_message == "EMAIL_NOT_FOUND":
                flash("No account found with this email.", "danger")
            elif error_message == "INVALID_EMAIL":
                flash("Enter a valid email address.", "danger")
            else:
                flash("Unable to send reset email.", "danger")
    except Exception as e:
        flash("Unable to send reset email.", "danger")
        
    return redirect(url_for("login"))
    

@app.route("/logout")
@login_required
def logout():
    session.clear()
    flash("You have been logged out.", "info")
    return redirect(url_for("login"))


@app.route("/dashboard")
@login_required
def dashboard():
    user_id = session["user_id"]
    patients = list(patients_ref.where("user_id", "==", user_id).stream())
    diagnoses = list(diagnoses_ref.where("user_id", "==", user_id).stream())

    patient_count = len(patients)
    report_count = len(diagnoses)
    oscc_count = sum(1 for doc in diagnoses if (doc.to_dict() or {}).get("diagnosis") == "OSCC")

    recent_reports = []
    for doc in sorted(diagnoses, key=lambda item: _created_at_value(item.to_dict() or {}), reverse=True)[:5]:
        data = doc.to_dict() or {}
        patient_doc = patients_ref.document(str(data.get("patient_id"))).get()
        patient_name = patient_doc.to_dict().get("full_name") if patient_doc.exists else ""
        payload = {
            "id": doc.id,
            "patient_id": data.get("patient_id"),
            "patient_name": patient_name,
            "diagnosis": data.get("diagnosis"),
            "confidence": data.get("confidence"),
            "image_filename": data.get("image_filename"),
            "created_at": data.get("created_at"),
        }
        recent_reports.append(payload)

    return render_template(
        "dashboard.html",
        patient_count=patient_count,
        report_count=report_count,
        oscc_count=oscc_count,
        recent_reports=recent_reports,
    )


@app.route("/patients")
@login_required
def patients():
    records = []
    for doc in patients_ref.where("user_id", "==", session["user_id"]).stream():
        data = _serialize_doc(doc)
        records.append(data)
    records.sort(key=lambda item: _created_at_value(item), reverse=True)
    return render_template("patients.html", patients=records)


@app.route("/patients/add", methods=["GET", "POST"])
@login_required
def add_patient():
    if request.method == "POST":
        fields = patient_form_data()
        if not fields["full_name"]:
            flash("Patient name is required.", "danger")
            return render_template("patient_form.html", patient=fields, mode="Add")

        patient_ref = patients_ref.document()
        patient_id = patient_ref.id
        patient_ref.set(
            {
                "id": patient_id,
                "user_id": session["user_id"],
                "full_name": fields["full_name"],
                "age": fields["age"],
                "gender": fields["gender"],
                "phone": fields["phone"],
                "email": fields["email"],
                "notes": fields["notes"],
                "created_at": firestore.SERVER_TIMESTAMP,
            }
        )
        flash("Patient record added.", "success")
        return redirect(url_for("patients"))

    return render_template("patient_form.html", patient={}, mode="Add")


@app.route("/patients/<patient_id>/edit", methods=["GET", "POST"])
@login_required
def edit_patient(patient_id):
    patient = get_patient_or_404(patient_id)
    if request.method == "POST":
        fields = patient_form_data()
        if not fields["full_name"]:
            flash("Patient name is required.", "danger")
            return render_template("patient_form.html", patient=fields, mode="Edit")

        patients_ref.document(str(patient_id)).set(
            {
                "id": patient_id,
                "user_id": session["user_id"],
                "full_name": fields["full_name"],
                "age": fields["age"],
                "gender": fields["gender"],
                "phone": fields["phone"],
                "email": fields["email"],
                "notes": fields["notes"],
            },
            merge=True,
        )
        flash("Patient record updated.", "success")
        return redirect(url_for("patients"))

    return render_template("patient_form.html", patient=patient, mode="Edit")


@app.route("/patients/<patient_id>/delete", methods=["POST"])
@login_required
def delete_patient(patient_id):
    get_patient_or_404(patient_id)
    patients_ref.document(str(patient_id)).delete()
    flash("Patient record deleted.", "info")
    return redirect(url_for("patients"))


def patient_form_data():
    age_raw = request.form.get("age", "").strip()
    age = int(age_raw) if age_raw.isdigit() else None
    return {
        "full_name": request.form.get("full_name", "").strip(),
        "age": age,
        "gender": request.form.get("gender", "").strip(),
        "phone": request.form.get("phone", "").strip(),
        "email": request.form.get("email", "").strip(),
        "notes": request.form.get("notes", "").strip(),
    }


def get_patient_or_404(patient_id):
    patient = _get_patient_by_id(patient_id)
    if patient is None or patient.get("user_id") != session["user_id"]:
        abort(404)
    return patient


@app.route("/diagnose", methods=["GET", "POST"])
@login_required
def diagnose():
    patients_list = []
    for doc in patients_ref.where("user_id", "==", session["user_id"]).stream():
        patients_list.append(_serialize_doc(doc))
    patients_list.sort(key=lambda item: item.get("full_name", ""))

    if request.method == "POST":
        patient_id = request.form.get("patient_id")
        patient = _get_patient_by_id(patient_id)
        file = request.files.get("image")

        if patient is None or patient.get("user_id") != session["user_id"]:
            flash("Please select a valid patient.", "danger")
            return render_template("diagnose.html", patients=patients_list)
        if file is None or file.filename == "":
            flash("Please upload an oral image.", "danger")
            return render_template("diagnose.html", patients=patients_list)
        if not allowed_file(file.filename):
            flash("Supported image formats are PNG, JPG, JPEG, and WEBP.", "danger")
            return render_template("diagnose.html", patients=patients_list)

        filename = secure_filename(file.filename)
        unique_filename = f"{uuid.uuid4().hex}_{filename}"
        saved_path = os.path.join(app.config["UPLOAD_FOLDER"], unique_filename)
        file.save(saved_path)

        try:
            diagnosis, confidence = predict_image(saved_path)
        except Exception as exc:
            os.remove(saved_path)
            flash(str(exc), "danger")
            return render_template("diagnose.html", patients=patients_list)

        try:
            if "user_id" not in session:
                raise ValueError("session['user_id'] is missing")
            if patient is None:
                raise ValueError("patient is missing")
            if patient.get("id") is None:
                raise ValueError("patient['id'] is missing")
            if patient.get("full_name") is None:
                raise ValueError("patient['full_name'] is missing")
            if not unique_filename:
                raise ValueError("unique_filename is missing")
            if diagnosis is None:
                raise ValueError("diagnosis is missing")
            if confidence is None:
                raise ValueError("confidence is missing")

            app.logger.info(
                "Saving diagnosis to Firestore: user_id=%s patient_id=%s patient_name=%s image=%s diagnosis=%s confidence=%s",
                session["user_id"],
                patient["id"],
                patient.get("full_name"),
                unique_filename,
                diagnosis,
                confidence,
            )

            diagnosis_ref = diagnoses_ref.document()
            diagnosis_data = {
                "id": diagnosis_ref.id,
                "user_id": session["user_id"],
                "patient_id": patient["id"],
                "patient_name": patient["full_name"],
                "image_filename": unique_filename,
                "diagnosis": diagnosis,
                "confidence": float(confidence),
                "created_at": firestore.SERVER_TIMESTAMP if db is not None else datetime.now(),
            }
            diagnosis_ref.set(diagnosis_data)
            app.logger.info("Diagnosis saved to Firestore successfully: %s", diagnosis_ref.id)
        except Exception as e:
            app.logger.exception("Firestore save failed")
            flash(str(e), "danger")
            return render_template(
                "diagnose.html",
                patients=patients_list,
                result={"patient": patient, "diagnosis": diagnosis, "confidence": confidence, "image_filename": unique_filename},
            )

        flash("Diagnosis completed and saved.", "success")
        return render_template(
            "diagnose.html",
            patients=patients_list,
            result={"patient": patient, "diagnosis": diagnosis, "confidence": confidence, "image_filename": unique_filename},
        )

    return render_template("diagnose.html", patients=patients_list)


@app.route("/reports")
@login_required
def reports():
    search = request.args.get("q", "").strip()
    records = []
    for doc in diagnoses_ref.where("user_id", "==", session["user_id"]).stream():
        data = doc.to_dict() or {}
        patient_doc = patients_ref.document(str(data.get("patient_id"))).get()
        patient_name = patient_doc.to_dict().get("full_name") if patient_doc.exists else ""
        patient_age = patient_doc.to_dict().get("age") if patient_doc.exists else None
        patient_gender = patient_doc.to_dict().get("gender") if patient_doc.exists else ""
        payload = {
            "id": data.get("id") or doc.id,
            "patient_id": data.get("patient_id"),
            "patient_name": patient_name,
            "age": patient_age,
            "gender": patient_gender,
            "image_filename": data.get("image_filename"),
            "diagnosis": data.get("diagnosis"),
            "confidence": data.get("confidence"),
            "created_at": data.get("created_at"),
        }
        records.append(payload)

    if search:
        records = [record for record in records if search.lower() in (record.get("patient_name") or "").lower()]

    records.sort(key=lambda item: _created_at_value(item), reverse=True)
    return render_template("reports.html", reports=records, search=search)


@app.route("/uploads/<path:filename>")
@login_required
def uploaded_file(filename):
    return send_from_directory(app.config["UPLOAD_FOLDER"], filename)


# ==========================================
# REST APIs for Android App
# ==========================================

@app.route("/api/login", methods=["POST"])
def api_login():
    data = request.get_json() or {}
    email = data.get("email", "").strip().lower()
    password = data.get("password", "")
    
    try:
        # Verify via Firebase REST API
        res = requests.post(
            f"https://identitytoolkit.googleapis.com/v1/accounts:signInWithPassword?key={FIREBASE_API_KEY}",
            json={"email": email, "password": password, "returnSecureToken": True}
        )
        if res.status_code == 200:
            user_id = res.json().get("localId")
            user_doc = users_ref.document(user_id).get()
            
            if user_doc.exists:
                user_data = user_doc.to_dict()
                session["user_id"] = user_id
                user_name = user_data.get("full_name", user_data.get("name", "Doctor"))
                return jsonify({"status": "success", "user_id": user_id, "user_name": user_name, "email": email})
            else:
                return jsonify({"status": "error", "message": "User profile not found."}), 404
        else:
            return jsonify({"status": "error", "message": "Invalid email or password."}), 401
    except Exception as e:
        return jsonify({"status": "error", "message": f"Login failed: {str(e)}"}), 500

@app.route("/api/signup", methods=["POST"])
def api_signup():
    data = request.get_json() or {}
    name = data.get("name", "").strip()
    email = data.get("email", "").strip().lower()
    password = data.get("password", "")
    
    if not name or not email or not password:
        return jsonify({"status": "error", "message": "All fields are required."}), 400
        
    try:
        existing = _get_user_by_email(email)
        if existing:
            return jsonify({"status": "error", "message": "Account exists."}), 400
            
        user_record = firebase_auth.create_user(email=email, password=password)
        user_id = user_record.uid
        
        users_ref.document(user_id).set(
            {
                "uid": user_id,
                "id": user_id,
                "full_name": name,
                "name": name,
                "email": email,
                "role": "Doctor",
                "created_at": firestore.SERVER_TIMESTAMP
            }
        )
    except Exception as e:
        return jsonify({"status": "error", "message": f"Signup failed: {str(e)}"}), 500
        
    return jsonify({"status": "success", "message": "Account created.", "user_id": user_id})

@app.route("/api/dashboard", methods=["GET"])
def api_dashboard():
    user_id = request.args.get("user_id") or session.get("user_id")
    if not user_id:
        return jsonify({"status": "error", "message": "Unauthorized"}), 401
        
    try:
        patients = list(patients_ref.where("user_id", "==", user_id).stream(timeout=5.0))
        diagnoses = list(diagnoses_ref.where("user_id", "==", user_id).stream(timeout=5.0))
    except Exception:
        return jsonify({"status": "error", "message": "Firebase service temporarily unavailable"}), 503
    
    recent = []
    for doc in sorted(diagnoses, key=lambda item: _created_at_value(item.to_dict() or {}), reverse=True)[:5]:
        data = doc.to_dict() or {}
        recent.append({
            "id": doc.id,
            "patient_id": data.get("patient_id"),
            "diagnosis": data.get("diagnosis"),
            "confidence": data.get("confidence")
        })
        
    return jsonify({
        "status": "success",
        "patient_count": len(patients),
        "report_count": len(diagnoses),
        "recent_reports": recent
    })

@app.route("/api/diagnose", methods=["POST"])
def api_diagnose():
    user_id = request.form.get("user_id") or session.get("user_id")
    patient_id = request.form.get("patient_id")
    file = request.files.get("image")
    
    if not user_id or not patient_id or not file:
        return jsonify({"status": "error", "message": "Missing required fields."}), 400
        
    patient = _get_patient_by_id(patient_id)
    if patient is None or patient.get("user_id") != user_id:
        return jsonify({"status": "error", "message": "Invalid patient."}), 400
        
    filename = secure_filename(file.filename)
    unique_filename = f"{uuid.uuid4().hex}_{filename}"
    saved_path = os.path.join(app.config["UPLOAD_FOLDER"], unique_filename)
    file.save(saved_path)
    
    try:
        diagnosis, confidence = predict_image(saved_path)
        diagnosis_ref = diagnoses_ref.document()
        diagnosis_ref.set({
            "id": diagnosis_ref.id,
            "uid": user_id,
            "user_id": user_id,
            "patient_id": patient["id"],
            "patient_name": patient["full_name"],
            "image_filename": unique_filename,
            "image_path": unique_filename,
            "diagnosis": diagnosis,
            "confidence": float(confidence),
            "created_at": firestore.SERVER_TIMESTAMP if db is not None else datetime.now(),
        })
        return jsonify({
            "status": "success",
            "diagnosis": diagnosis,
            "confidence": confidence,
            "image_filename": unique_filename
        })
    except ValueError as ve:
        os.remove(saved_path)
        # Catch explicit validation failures (e.g. not a valid microscopic image)
        return jsonify({
            "status": "invalid_image",
            "message": str(ve)
        }), 400
    except Exception as exc:
        os.remove(saved_path)
        return jsonify({"status": "error", "message": str(exc)}), 500

@app.route("/api/patients", methods=["GET"])
def api_patients():
    user_id = request.args.get("user_id") or session.get("user_id")
    if not user_id:
        return jsonify({"status": "error", "message": "Unauthorized"}), 401
    records = []
    for doc in patients_ref.where("user_id", "==", user_id).stream():
        records.append(_serialize_doc(doc))
    return jsonify({"status": "success", "patients": records})

@app.route("/api/patients/add", methods=["POST"])
def api_add_patient():
    data = request.get_json() or {}
    user_id = data.get("user_id") or session.get("user_id")
    if not user_id:
        return jsonify({"status": "error", "message": "Unauthorized"}), 401
    full_name = data.get("full_name", "").strip()
    if not full_name:
        return jsonify({"status": "error", "message": "Patient name is required."}), 400
    
    patient_ref = patients_ref.document()
    patient_id = patient_ref.id
    patient_ref.set({
        "id": patient_id,
        "user_id": user_id,
        "full_name": full_name,
        "age": data.get("age"),
        "gender": data.get("gender", "").strip(),
        "phone": data.get("phone", "").strip(),
        "email": data.get("email", "").strip(),
        "notes": data.get("notes", "").strip(),
        "created_at": firestore.SERVER_TIMESTAMP if db is not None else datetime.now(),
    })
    return jsonify({"status": "success", "message": "Patient added successfully.", "id": patient_id})

@app.route("/api/patients/<patient_id>/edit", methods=["PUT", "POST"])
def api_edit_patient(patient_id):
    data = request.get_json() or {}
    user_id = data.get("user_id") or session.get("user_id")
    if not user_id:
        return jsonify({"status": "error", "message": "Unauthorized"}), 401
    patient = _get_patient_by_id(patient_id)
    if patient is None or patient.get("user_id") != user_id:
        return jsonify({"status": "error", "message": "Patient not found or unauthorized."}), 404
    full_name = data.get("full_name", "").strip()
    if not full_name:
        return jsonify({"status": "error", "message": "Patient name is required."}), 400

    patients_ref.document(str(patient_id)).set({
        "id": patient_id,
        "user_id": user_id,
        "full_name": full_name,
        "age": data.get("age"),
        "gender": data.get("gender", "").strip(),
        "phone": data.get("phone", "").strip(),
        "email": data.get("email", "").strip(),
        "notes": data.get("notes", "").strip(),
    }, merge=True)
    return jsonify({"status": "success", "message": "Patient updated successfully."})

@app.route("/api/patients/<patient_id>/delete", methods=["DELETE", "POST"])
def api_delete_patient(patient_id):
    user_id = request.args.get("user_id") or (request.get_json() or {}).get("user_id") or session.get("user_id")
    if not user_id:
        return jsonify({"status": "error", "message": "Unauthorized"}), 401
    patient = _get_patient_by_id(patient_id)
    if patient is None or patient.get("user_id") != user_id:
        return jsonify({"status": "error", "message": "Patient not found or unauthorized."}), 404
    patients_ref.document(str(patient_id)).delete()
    return jsonify({"status": "success", "message": "Patient deleted successfully."})

@app.route("/api/reports", methods=["GET"])
def api_reports():
    user_id = request.args.get("user_id") or session.get("user_id")
    if not user_id:
        return jsonify({"status": "error", "message": "Unauthorized"}), 401
    records = []
    try:
        for doc in diagnoses_ref.where("user_id", "==", user_id).stream(timeout=5.0):
            data = doc.to_dict() or {}
            data["id"] = data.get("id") or doc.id
            records.append(data)
    except Exception:
        return jsonify({"status": "error", "message": "Firebase service temporarily unavailable"}), 503
        
    return jsonify({"status": "success", "reports": records})


@app.route("/api/health", methods=["GET"])
def api_health():
    return jsonify({"status": "success", "message": "API is reachable"})


if __name__ == "__main__":
    load_model()
    app.run(debug=False, host="0.0.0.0", port=5000)
