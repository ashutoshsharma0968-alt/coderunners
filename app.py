import os
from datetime import datetime, timedelta
from flask import Flask, request, jsonify, send_from_directory, url_for
from flask_sqlalchemy import SQLAlchemy
from flask_migrate import Migrate
from flask_jwt_extended import (
    JWTManager, create_access_token, jwt_required, get_jwt_identity
)
from werkzeug.utils import secure_filename
from flask_socketio import SocketIO, emit
from dotenv import load_dotenv
from PIL import Image

load_dotenv()

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
UPLOAD_FOLDER = os.path.join(BASE_DIR, "uploads")
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

app = Flask(__name__)
app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///" + os.path.join(BASE_DIR, "app.db")
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["JWT_SECRET_KEY"] = os.getenv("JWT_SECRET_KEY", "change-this-secret")
app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024  # 16 MB limit

db = SQLAlchemy(app)
migrate = Migrate(app, db)
jwt = JWTManager(app)
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="eventlet")

# -----------------------
# Models
# -----------------------
class Student(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(180), unique=True, nullable=False)
    name = db.Column(db.String(120), nullable=False)
    password = db.Column(db.String(200), nullable=False)  # store hashed in prod
    department = db.Column(db.String(80), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

class LostItem(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    student_id = db.Column(db.Integer, db.ForeignKey("student.id"), nullable=False)
    title = db.Column(db.String(200), nullable=False)
    description = db.Column(db.Text, nullable=True)
    image_filename = db.Column(db.String(300), nullable=True)
    location = db.Column(db.String(200), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

class CanteenItem(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(200), nullable=False)
    price = db.Column(db.Float, nullable=False)
    available = db.Column(db.Boolean, default=True)
    quantity = db.Column(db.Integer, default=0)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

class CanteenOrder(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    student_id = db.Column(db.Integer, db.ForeignKey("student.id"), nullable=False)
    item_id = db.Column(db.Integer, db.ForeignKey("canteen_item.id"), nullable=False)
    quantity = db.Column(db.Integer, default=1)
    status = db.Column(db.String(80), default="placed")  # placed, preparing, ready, picked
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

class EventNotification(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(200), nullable=False)
    description = db.Column(db.Text, nullable=True)
    department = db.Column(db.String(80), nullable=True)
    event_time = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

# -----------------------
# Helpers
# -----------------------
def allowed_file(filename):
    ALLOWED = {"png", "jpg", "jpeg", "gif"}
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED

def save_image(file_storage, prefix="img"):
    if file_storage and allowed_file(file_storage.filename):
        filename = secure_filename(file_storage.filename)
        timestamp = datetime.utcnow().strftime("%Y%m%d%H%M%S%f")
        filename = f"{prefix}_{timestamp}_{filename}"
        path = os.path.join(app.config["UPLOAD_FOLDER"], filename)
        file_storage.save(path)
        # Optionally create a thumbnail
        try:
            img = Image.open(path)
            img.thumbnail((1024, 1024))
            img.save(path)
        except Exception:
            pass
        return filename
    return None

def student_to_dict(s):
    return {"id": s.id, "email": s.email, "name": s.name, "department": s.department}

# -----------------------
# Auth routes
# -----------------------
@app.route("/auth/register", methods=["POST"])
def register():
    data = request.json or {}
    email = data.get("email")
    name = data.get("name")
    password = data.get("password")
    department = data.get("department")
    if not email or not password or not name:
        return jsonify({"msg": "email, name and password required"}), 400
    if Student.query.filter_by(email=email).first():
        return jsonify({"msg": "email already registered"}), 400
    # NOTE: For production, hash passwords (bcrypt). Here we store plain for brevity.
    s = Student(email=email, name=name, password=password, department=department)
    db.session.add(s)
    db.session.commit()
    return jsonify({"msg": "registered", "student": student_to_dict(s)}), 201

@app.route("/auth/login", methods=["POST"])
def login():
    data = request.json or {}
    email = data.get("email")
    password = data.get("password")
    if not email or not password:
        return jsonify({"msg": "email and password required"}), 400
    s = Student.query.filter_by(email=email).first()
    if not s or s.password != password:
        return jsonify({"msg": "invalid credentials"}), 401
    access_token = create_access_token(identity=s.id, expires_delta=timedelta(days=7))
    return jsonify({"access_token": access_token, "student": student_to_dict(s)}), 200

# -----------------------
# Lost & Found
# -----------------------
@app.route("/lost", methods=["POST"])
@jwt_required()
def create_lost():
    student_id = get_jwt_identity()
    title = request.form.get("title")
    description = request.form.get("description")
    location = request.form.get("location")
    file = request.files.get("image")
    if not title:
        return jsonify({"msg": "title required"}), 400
    filename = save_image(file, prefix="lost") if file else None
    li = LostItem(student_id=student_id, title=title, description=description, image_filename=filename, location=location)
    db.session.add(li)
    db.session.commit()
    return jsonify({"msg": "lost item created", "id": li.id}), 201

@app.route("/lost", methods=["GET"])
def list_lost():
    items = LostItem.query.order_by(LostItem.created_at.desc()).all()
    out = []
    for it in items:
        img_url = url_for("uploaded_file", filename=it.image_filename, _external=True) if it.image_filename else None
        out.append({
            "id": it.id,
            "title": it.title,
            "description": it.description,
            "location": it.location,
            "image_url": img_url,
            "created_at": it.created_at.isoformat()
        })
    return jsonify(out), 200

@app.route("/uploads/<path:filename>")
def uploaded_file(filename):
    return send_from_directory(app.config["UPLOAD_FOLDER"], filename)

# -----------------------
# Canteen: menu and orders
# -----------------------
@app.route("/canteen/menu", methods=["POST"])
@jwt_required()
def add_menu_item():
    data = request.json or {}
    name = data.get("name")
    price = data.get("price", 0.0)
    quantity = data.get("quantity", 0)
    available = data.get("available", True)
    if not name:
        return jsonify({"msg": "name required"}), 400
    item = CanteenItem(name=name, price=float(price), quantity=int(quantity), available=bool(available))
    db.session.add(item)
    db.session.commit()
    # broadcast update
    socketio.emit("canteen_update", {"action": "add", "item": {"id": item.id, "name": item.name, "price": item.price, "quantity": item.quantity, "available": item.available}})
    return jsonify({"msg": "menu item added", "id": item.id}), 201

@app.route("/canteen/menu", methods=["GET"])
def get_menu():
    items = CanteenItem.query.order_by(CanteenItem.name).all()
    out = [{"id": i.id, "name": i.name, "price": i.price, "quantity": i.quantity, "available": i.available} for i in items]
    return jsonify(out), 200

@app.route("/canteen/order", methods=["POST"])
@jwt_required()
def place_order():
    student_id = get_jwt_identity()
    data = request.json or {}
    item_id = data.get("item_id")
    qty = int(data.get("quantity", 1))
    if not item_id:
        return jsonify({"msg": "item_id required"}), 400
    item = CanteenItem.query.get(item_id)
    if not item or not item.available or item.quantity < qty:
        return jsonify({"msg": "item not available or insufficient quantity"}), 400
    # reduce quantity
    item.quantity -= qty
    if item.quantity <= 0:
        item.available = False
    order = CanteenOrder(student_id=student_id, item_id=item.id, quantity=qty, status="placed")
    db.session.add(order)
    db.session.commit()
    socketio.emit("canteen_update", {"action": "order", "item_id": item.id, "new_quantity": item.quantity})
    return jsonify({"msg": "order placed", "order_id": order.id}), 201

@app.route("/canteen/orders", methods=["GET"])
@jwt_required()
def order_history():
    student_id = get_jwt_identity()
    orders = CanteenOrder.query.filter_by(student_id=student_id).order_by(CanteenOrder.created_at.desc()).all()
    out = []
    for o in orders:
        item = CanteenItem.query.get(o.item_id)
        out.append({
            "order_id": o.id,
            "item": {"id": item.id, "name": item.name} if item else None,
            "quantity": o.quantity,
            "status": o.status,
            "created_at": o.created_at.isoformat()
        })
    return jsonify(out), 200

# -----------------------
# Events
# -----------------------
@app.route("/events", methods=["POST"])
@jwt_required()
def create_event():
    data = request.json or {}
    title = data.get("title")
    description = data.get("description")
    department = data.get("department")
    event_time = data.get("event_time")  # ISO string optional
    if not title:
        return jsonify({"msg": "title required"}), 400
    et = None
    if event_time:
        try:
            et = datetime.fromisoformat(event_time)
        except Exception:
            et = None
    ev = EventNotification(title=title, description=description, department=department, event_time=et)
    db.session.add(ev)
    db.session.commit()
    # For demo: broadcast event creation
    socketio.emit("event_created", {"id": ev.id, "title": ev.title, "department": ev.department})
    return jsonify({"msg": "event created", "id": ev.id}), 201

@app.route("/events", methods=["GET"])
def list_events():
    dept = request.args.get("department")
    q = EventNotification.query.order_by(EventNotification.created_at.desc())
    if dept:
        q = q.filter_by(department=dept)
    events = q.all()
    out = []
    for e in events:
        out.append({
            "id": e.id,
            "title": e.title,
            "description": e.description,
            "department": e.department,
            "event_time": e.event_time.isoformat() if e.event_time else None,
            "created_at": e.created_at.isoformat()
        })
    return jsonify(out), 200

# -----------------------
# SocketIO handlers (optional)
# -----------------------
@socketio.on("connect")
def handle_connect():
    emit("connected", {"msg": "connected to Smart Campus Socket"})

# -----------------------
# Admin helper (simple)
# -----------------------
@app.route("/admin/reset", methods=["POST"])
def reset_db():
    # Danger: for dev only
    db.drop_all()
    db.create_all()
    return jsonify({"msg": "db reset"}), 200

# -----------------------
# Run
# -----------------------
if __name__ == "__main__":
    # Create DB if not exists
    with app.app_context():
        db.create_all()
    # Use socketio.run to enable websockets
    socketio.run(app, host="0.0.0.0", port=5000)