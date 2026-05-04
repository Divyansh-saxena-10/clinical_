"""
Patient Management System - Flask Application
Updated:
  - Registration number entered manually by doctor
  - Prescription (up to 6 medicines) + Bill included in registration form
  - WhatsApp notification via Twilio when prescription ends in <=2 days
"""

import os
import io
import csv
from datetime import datetime, date, timedelta
from functools import wraps

from flask import (
    Flask, render_template, request, redirect, url_for,
    flash, session, make_response, abort, Response
)
from flask_sqlalchemy import SQLAlchemy
from flask_login import (
    LoginManager, UserMixin, login_user, logout_user,
    login_required, current_user
)
from werkzeug.security import generate_password_hash, check_password_hash
from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.units import cm
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, HRFlowable
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
from dotenv import load_dotenv

from apscheduler.schedulers.background import BackgroundScheduler
import atexit

# ─── Configuration ────────────────────────────────────────────────────────────

load_dotenv()

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'clinic-secret-key-change-in-production')

database_url = os.environ.get('DATABASE_URL', 'sqlite:///clinic.db')
if database_url.startswith('postgres://'):
    database_url = database_url.replace('postgres://', 'postgresql://', 1)
app.config['SQLALCHEMY_DATABASE_URI'] = database_url
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)
login_manager = LoginManager(app)
login_manager.login_view = 'login'
login_manager.login_message = 'Please log in to access this page.'
login_manager.login_message_category = 'warning'

# Clinic / Doctor constants
CLINIC_NAME    = os.environ.get('CLINIC_NAME',    'HealthCare Clinic')
CLINIC_ADDRESS = os.environ.get('CLINIC_ADDRESS', '123 Medical Street, City - 000000')
CLINIC_PHONE   = os.environ.get('CLINIC_PHONE',   '+91 98765 43210')
DOCTOR_NAME    = os.environ.get('DOCTOR_NAME',    'Dr. Anilesh Tiwari')
DOCTOR_DEGREE  = os.environ.get('DOCTOR_DEGREE',  'MBBS, MD (General Medicine)')

# Twilio WhatsApp credentials (set in .env)
TWILIO_SID        = os.environ.get('TWILIO_SID', '')
TWILIO_TOKEN      = os.environ.get('TWILIO_TOKEN', '')
TWILIO_WHATSAPP   = os.environ.get('TWILIO_WHATSAPP', 'whatsapp:+14155238886')  # Twilio sandbox number


# ─── Models ───────────────────────────────────────────────────────────────────

class User(UserMixin, db.Model):
    __tablename__ = 'users'
    id            = db.Column(db.Integer, primary_key=True)
    username      = db.Column(db.String(80), unique=True, nullable=False)
    password_hash = db.Column(db.String(256), nullable=False)
    name          = db.Column(db.String(120), nullable=False, default='Doctor')

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)


class Patient(db.Model):
    __tablename__ = 'patients'
    id                  = db.Column(db.Integer, primary_key=True)
    serial_number       = db.Column(db.String(20), unique=True, nullable=False)
    registration_number = db.Column(db.String(50), unique=True, nullable=False)  # doctor enters manually
    name                = db.Column(db.String(120), nullable=False)
    age                 = db.Column(db.Integer, nullable=False)
    sex                 = db.Column(db.String(10), nullable=False)
    address             = db.Column(db.Text, nullable=True)
    mobile              = db.Column(db.String(15), nullable=False)
    code                = db.Column(db.String(30), nullable=True)
    complaint           = db.Column(db.Text, nullable=True)
    bp                  = db.Column(db.String(20), nullable=True)
    weight              = db.Column(db.String(20), nullable=True)
    medicine            = db.Column(db.Text, nullable=True)
    advice              = db.Column(db.Text, nullable=True)
    created_at          = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at          = db.Column(db.DateTime, default=datetime.utcnow)

    visits = db.relationship('Visit', backref='patient', lazy=True,
                             cascade='all, delete-orphan', order_by='Visit.visit_date.desc()')
    bills  = db.relationship('Bill',  backref='patient', lazy=True,
                             cascade='all, delete-orphan')

    def to_dict(self):
        return {
            'id': self.id,
            'serial_number': self.serial_number,
            'registration_number': self.registration_number,
            'name': self.name,
            'age': self.age,
            'sex': self.sex,
            'mobile': self.mobile,
            'address': self.address or '',
            'created_at': self.created_at.strftime('%d %b %Y') if self.created_at else ''
        }


class Visit(db.Model):
    __tablename__ = 'visits'
    id         = db.Column(db.Integer, primary_key=True)
    patient_id = db.Column(db.Integer, db.ForeignKey('patients.id'), nullable=False)
    visit_date = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    complaint  = db.Column(db.Text, nullable=True)
    diagnosis  = db.Column(db.Text, nullable=True)
    bp         = db.Column(db.String(20), nullable=True)
    weight     = db.Column(db.String(20), nullable=True)
    advice     = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    prescriptions = db.relationship('Prescription', backref='visit', lazy=True,
                                    cascade='all, delete-orphan')


class Prescription(db.Model):
    __tablename__ = 'prescriptions'
    id            = db.Column(db.Integer, primary_key=True)
    visit_id      = db.Column(db.Integer, db.ForeignKey('visits.id'), nullable=False)
    medicine_name = db.Column(db.String(200), nullable=False)
    dosage        = db.Column(db.String(50), nullable=True)
    days          = db.Column(db.Integer, nullable=True)
    start_date    = db.Column(db.Date, default=date.today, nullable=True)
    instructions  = db.Column(db.String(300), nullable=True)
    created_at    = db.Column(db.DateTime, default=datetime.utcnow)
    # Track if WhatsApp reminder already sent
    reminder_sent = db.Column(db.Boolean, default=False)

    @property
    def end_date(self):
        if self.start_date and self.days:
            return self.start_date + timedelta(days=self.days)
        return None

    @property
    def is_active(self):
        if self.end_date:
            return date.today() <= self.end_date
        return False

    @property
    def days_remaining(self):
        if self.end_date:
            remaining = (self.end_date - date.today()).days
            return max(remaining, 0)
        return None


class Bill(db.Model):
    __tablename__ = 'bills'
    id               = db.Column(db.Integer, primary_key=True)
    patient_id       = db.Column(db.Integer, db.ForeignKey('patients.id'), nullable=False)
    visit_id         = db.Column(db.Integer, db.ForeignKey('visits.id'), nullable=True)
    consultation_fee = db.Column(db.Float, default=0.0)
    medicine_cost    = db.Column(db.Float, default=0.0)
    other_charges    = db.Column(db.Float, default=0.0)
    total            = db.Column(db.Float, default=0.0)
    notes            = db.Column(db.String(300), nullable=True)
    created_at       = db.Column(db.DateTime, default=datetime.utcnow)

    visit = db.relationship('Visit', backref='bill', uselist=False)


@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))


# ─── Helpers ──────────────────────────────────────────────────────────────────

def generate_serial_number():
    """Auto serial number SN0001, SN0002..."""
    last = Patient.query.order_by(Patient.id.desc()).first()
    if last:
        try:
            num = int(last.serial_number.replace('SN', '')) + 1
        except Exception:
            num = Patient.query.count() + 1
    else:
        num = 1
    return f"SN{num:04d}"


# ─── WhatsApp Notification ────────────────────────────────────────────────────

def send_whatsapp(mobile, message):
    """
    Send WhatsApp message via Twilio.
    mobile: 10-digit Indian number like 9876543210
    """
    if not TWILIO_SID or not TWILIO_TOKEN:
        print(f"[WHATSAPP] Twilio not configured. Message to {mobile}: {message}")
        return False

    try:
        from twilio.rest import Client
        client = Client(TWILIO_SID, TWILIO_TOKEN)
        # Format number: whatsapp:+919876543210
        to_number = f"whatsapp:+91{mobile.strip()}"
        msg = client.messages.create(
            body=message,
            from_=TWILIO_WHATSAPP,
            to=to_number
        )
        print(f"[WHATSAPP SENT] SID: {msg.sid} → {to_number}")
        return True
    except Exception as e:
        print(f"[WHATSAPP ERROR] {e}")
        return False


def check_prescription_reminders():
    """
    Daily background job:
    - Finds prescriptions ending in 0, 1, or 2 days
    - Sends WhatsApp reminder to patient (only once)
    """
    with app.app_context():
        try:
            today = date.today()
            reminder_window = today + timedelta(days=2)
            prescriptions = Prescription.query.filter_by(reminder_sent=False).all()

            for rx in prescriptions:
                if rx.end_date and today <= rx.end_date <= reminder_window:
                    patient = rx.visit.patient
                    days_left = rx.days_remaining

                    message = (
                        f"Namaskar {patient.name} ji 🙏\n\n"
                        f"Aapki dawai *{rx.medicine_name}* ka course "
                        f"{'aaj khatam ho raha hai' if days_left == 0 else f'{days_left} din mein khatam ho raha hai'}.\n\n"
                        f"Kripya clinic mein sampark karein.\n\n"
                        f"— {DOCTOR_NAME}\n{CLINIC_NAME}\n📞 {CLINIC_PHONE}"
                    )

                    success = send_whatsapp(patient.mobile, message)
                    if success:
                        rx.reminder_sent = True
                        db.session.commit()
                        print(f"[REMINDER SENT] {patient.name} → {rx.medicine_name}")
                    else:
                        print(f"[REMINDER FAILED] {patient.name} → {rx.medicine_name}")

        except Exception as e:
            print(f"[SCHEDULER ERROR] {e}")


# Start background scheduler
scheduler = BackgroundScheduler()
scheduler.add_job(
    func=check_prescription_reminders,
    trigger='interval',
    hours=24,
    id='prescription_reminder',
    name='Daily WhatsApp Prescription Reminder',
    next_run_time=datetime.now()
)
scheduler.start()
atexit.register(lambda: scheduler.shutdown())


# ─── Auth Routes ──────────────────────────────────────────────────────────────

@app.route('/')
def index():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))
    return redirect(url_for('login'))


@app.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))

    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')

        if not username or not password:
            flash('Please enter both username and password.', 'danger')
            return render_template('login.html')

        user = User.query.filter_by(username=username).first()
        if user and user.check_password(password):
            login_user(user, remember=request.form.get('remember'))
            flash(f'Welcome back, {user.name}!', 'success')
            next_page = request.args.get('next')
            return redirect(next_page or url_for('dashboard'))
        else:
            flash('Invalid username or password.', 'danger')

    return render_template('login.html')


@app.route('/logout')
@login_required
def logout():
    logout_user()
    flash('Logged out successfully.', 'info')
    return redirect(url_for('login'))


@app.route('/change-password', methods=['GET', 'POST'])
@login_required
def change_password():
    if request.method == 'POST':
        old_password     = request.form.get('old_password', '')
        new_password     = request.form.get('new_password', '').strip()
        confirm_password = request.form.get('confirm_password', '').strip()

        if not current_user.check_password(old_password):
            flash('Current password is incorrect.', 'danger')
            return render_template('change_password.html', clinic_name=CLINIC_NAME)
        if len(new_password) < 6:
            flash('New password must be at least 6 characters.', 'danger')
            return render_template('change_password.html', clinic_name=CLINIC_NAME)
        if new_password != confirm_password:
            flash('New passwords do not match.', 'danger')
            return render_template('change_password.html', clinic_name=CLINIC_NAME)

        current_user.set_password(new_password)
        db.session.commit()
        flash('Password changed successfully!', 'success')
        return redirect(url_for('dashboard'))

    return render_template('change_password.html', clinic_name=CLINIC_NAME)


# ─── Dashboard ────────────────────────────────────────────────────────────────

@app.route('/dashboard')
@login_required
def dashboard():
    today = date.today()
    total_patients  = Patient.query.count()
    today_patients  = Patient.query.filter(
        db.func.date(Patient.created_at) == today
    ).count()
    total_visits    = Visit.query.count()
    recent_patients = Patient.query.order_by(Patient.created_at.desc()).limit(10).all()

    return render_template(
        'dashboard.html',
        total_patients=total_patients,
        today_patients=today_patients,
        total_visits=total_visits,
        recent_patients=recent_patients,
        today=today.strftime('%d %B %Y'),
        clinic_name=CLINIC_NAME
    )


# ─── CSV Export ───────────────────────────────────────────────────────────────

@app.route('/patients/export-csv')
@login_required
def export_csv():
    patients = Patient.query.order_by(Patient.created_at.desc()).all()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        'Serial No', 'Registration No', 'Name', 'Age', 'Sex',
        'Mobile', 'Address', 'Code', 'Blood Pressure', 'Weight',
        'Chief Complaint', 'Total Visits', 'Registered On'
    ])
    for p in patients:
        writer.writerow([
            p.serial_number, p.registration_number, p.name, p.age, p.sex,
            p.mobile, p.address or '', p.code or '', p.bp or '', p.weight or '',
            p.complaint or '', len(p.visits),
            p.created_at.strftime('%d-%m-%Y') if p.created_at else ''
        ])
    csv_data = '\ufeff' + output.getvalue()
    response = make_response(csv_data)
    response.headers['Content-Type'] = 'text/csv; charset=utf-8'
    response.headers['Content-Disposition'] = \
        f'attachment; filename=patients_{date.today().strftime("%Y%m%d")}.csv'
    return response


# ─── Add Patient (with Prescription + Bill in same form) ─────────────────────

@app.route('/patients/add', methods=['GET', 'POST'])
@login_required
def add_patient():
    """
    Single registration form that captures:
    - Patient demographics
    - Registration number (entered manually by doctor)
    - Clinical info (BP, weight, complaint, advice)
    - Prescriptions (up to 6 medicines)
    - Billing (consultation fee, medicine cost, other charges)
    """
    if request.method == 'POST':
        # ── Patient fields ──
        name                = request.form.get('name', '').strip()
        age                 = request.form.get('age', '').strip()
        sex                 = request.form.get('sex', '').strip()
        address             = request.form.get('address', '').strip()
        mobile              = request.form.get('mobile', '').strip()
        code                = request.form.get('code', '').strip()
        registration_number = request.form.get('registration_number', '').strip()
        complaint           = request.form.get('complaint', '').strip()
        bp                  = request.form.get('bp', '').strip()
        weight              = request.form.get('weight', '').strip()
        advice              = request.form.get('advice', '').strip()

        # ── Validation ──
        errors = []
        if not name:
            errors.append('Patient name is required.')
        if not age or not age.isdigit() or int(age) <= 0 or int(age) > 150:
            errors.append('Valid age is required (1–150).')
        if not sex:
            errors.append('Sex is required.')
        if not mobile or len(mobile) < 10:
            errors.append('Valid 10-digit mobile number is required.')
        if not registration_number:
            errors.append('Registration number is required.')
        else:
            existing = Patient.query.filter_by(registration_number=registration_number).first()
            if existing:
                errors.append(f'Registration number "{registration_number}" already exists.')

        if errors:
            for err in errors:
                flash(err, 'danger')
            return render_template('add_patient.html', form_data=request.form, clinic_name=CLINIC_NAME)

        # ── Create Patient ──
        patient = Patient(
            serial_number=generate_serial_number(),
            registration_number=registration_number,
            name=name, age=int(age), sex=sex,
            address=address, mobile=mobile, code=code,
            complaint=complaint, bp=bp, weight=weight,
            advice=advice
        )
        db.session.add(patient)
        db.session.flush()  # get patient.id

        # ── Create Visit ──
        visit = Visit(
            patient_id=patient.id,
            complaint=complaint,
            bp=bp,
            weight=weight,
            advice=advice,
            visit_date=datetime.utcnow()
        )
        db.session.add(visit)
        db.session.flush()  # get visit.id

        # ── Save Prescriptions (up to 6) ──
        medicine_names = request.form.getlist('medicine_name[]')
        dosages        = request.form.getlist('dosage[]')
        days_list      = request.form.getlist('days[]')
        instructions   = request.form.getlist('instructions[]')

        for i, med_name in enumerate(medicine_names):
            med_name = med_name.strip()
            if not med_name:
                continue
            days_val = None
            if i < len(days_list) and days_list[i].strip().isdigit():
                days_val = int(days_list[i].strip())

            rx = Prescription(
                visit_id=visit.id,
                medicine_name=med_name,
                dosage=dosages[i].strip() if i < len(dosages) else '',
                days=days_val,
                start_date=date.today(),
                instructions=instructions[i].strip() if i < len(instructions) else ''
            )
            db.session.add(rx)

        # ── Save Bill ──
        try:
            consultation_fee = float(request.form.get('consultation_fee', 0) or 0)
            medicine_cost    = float(request.form.get('medicine_cost', 0) or 0)
            other_charges    = float(request.form.get('other_charges', 0) or 0)
        except ValueError:
            consultation_fee = medicine_cost = other_charges = 0.0

        total = consultation_fee + medicine_cost + other_charges
        bill_notes = request.form.get('bill_notes', '').strip()

        if total > 0:  # only save bill if any amount entered
            bill = Bill(
                patient_id=patient.id,
                visit_id=visit.id,
                consultation_fee=consultation_fee,
                medicine_cost=medicine_cost,
                other_charges=other_charges,
                total=total,
                notes=bill_notes
            )
            db.session.add(bill)

        try:
            db.session.commit()
            flash(f'Patient {name} registered successfully! (Reg: {registration_number})', 'success')
            return redirect(url_for('patient_detail', patient_id=patient.id))
        except Exception as e:
            db.session.rollback()
            flash('An error occurred while saving. Please try again.', 'danger')
            return render_template('add_patient.html', form_data=request.form, clinic_name=CLINIC_NAME)

    return render_template('add_patient.html', form_data={}, clinic_name=CLINIC_NAME)


# ─── Patient List & Search ────────────────────────────────────────────────────

@app.route('/patients')
@login_required
def patient_list():
    page     = request.args.get('page', 1, type=int)
    search   = request.args.get('search', '').strip()
    per_page = 15

    query = Patient.query
    if search:
        query = query.filter(
            db.or_(
                Patient.name.ilike(f'%{search}%'),
                Patient.mobile.ilike(f'%{search}%'),
                Patient.registration_number.ilike(f'%{search}%'),
                Patient.serial_number.ilike(f'%{search}%')
            )
        )

    pagination = query.order_by(Patient.created_at.desc()).paginate(
        page=page, per_page=per_page, error_out=False
    )

    return render_template(
        'patient_list.html',
        patients=pagination.items,
        pagination=pagination,
        search=search
    )


# ─── Patient Detail ───────────────────────────────────────────────────────────

@app.route('/patients/<int:patient_id>')
@login_required
def patient_detail(patient_id):
    patient = Patient.query.get_or_404(patient_id)
    visits  = Visit.query.filter_by(patient_id=patient_id)\
                         .order_by(Visit.visit_date.desc()).all()
    bills   = Bill.query.filter_by(patient_id=patient_id)\
                        .order_by(Bill.created_at.desc()).all()
    total_billed = sum(b.total for b in bills)
    return render_template(
        'patient_detail.html',
        patient=patient,
        visits=visits,
        bills=bills,
        total_billed=total_billed,
        clinic_name=CLINIC_NAME
    )


# ─── Edit Patient ─────────────────────────────────────────────────────────────

@app.route('/patients/<int:patient_id>/edit', methods=['GET', 'POST'])
@login_required
def edit_patient(patient_id):
    patient = Patient.query.get_or_404(patient_id)

    if request.method == 'POST':
        name   = request.form.get('name', '').strip()
        age    = request.form.get('age', '').strip()
        sex    = request.form.get('sex', '').strip()
        mobile = request.form.get('mobile', '').strip()
        new_reg = request.form.get('registration_number', '').strip()

        errors = []
        if not name:
            errors.append('Patient name is required.')
        if not age or not age.isdigit() or int(age) <= 0 or int(age) > 150:
            errors.append('Valid age is required.')
        if not sex:
            errors.append('Sex is required.')
        if not mobile or len(mobile) < 10:
            errors.append('Valid mobile number is required.')
        if not new_reg:
            errors.append('Registration number is required.')
        else:
            existing = Patient.query.filter(
                Patient.registration_number == new_reg,
                Patient.id != patient_id
            ).first()
            if existing:
                errors.append(f'Registration number "{new_reg}" already used by another patient.')

        if errors:
            for err in errors:
                flash(err, 'danger')
            return render_template('add_patient.html', form_data=request.form,
                                   patient=patient, editing=True, clinic_name=CLINIC_NAME)

        patient.name                = name
        patient.age                 = int(age)
        patient.sex                 = sex
        patient.address             = request.form.get('address', '').strip()
        patient.mobile              = mobile
        patient.code                = request.form.get('code', '').strip()
        patient.registration_number = new_reg
        patient.complaint           = request.form.get('complaint', '').strip()
        patient.bp                  = request.form.get('bp', '').strip()
        patient.weight              = request.form.get('weight', '').strip()
        patient.advice              = request.form.get('advice', '').strip()
        patient.updated_at          = datetime.utcnow()

        try:
            db.session.commit()
            flash('Patient record updated successfully!', 'success')
            return redirect(url_for('patient_detail', patient_id=patient.id))
        except Exception:
            db.session.rollback()
            flash('Error updating record. Please try again.', 'danger')

    return render_template('add_patient.html', form_data=patient,
                           patient=patient, editing=True, clinic_name=CLINIC_NAME)


# ─── Delete Patient ───────────────────────────────────────────────────────────

@app.route('/patients/<int:patient_id>/delete', methods=['POST'])
@login_required
def delete_patient(patient_id):
    patient = Patient.query.get_or_404(patient_id)
    name = patient.name
    try:
        db.session.delete(patient)
        db.session.commit()
        flash(f'Patient record for {name} deleted.', 'warning')
    except Exception:
        db.session.rollback()
        flash('Could not delete patient. Please try again.', 'danger')
    return redirect(url_for('patient_list'))


# ─── Visit Routes ─────────────────────────────────────────────────────────────

@app.route('/patients/<int:patient_id>/visits/add', methods=['GET', 'POST'])
@login_required
def add_visit(patient_id):
    """Add follow-up visit with prescriptions."""
    patient = Patient.query.get_or_404(patient_id)

    if request.method == 'POST':
        complaint = request.form.get('complaint', '').strip()
        diagnosis = request.form.get('diagnosis', '').strip()
        bp        = request.form.get('bp', '').strip()
        weight    = request.form.get('weight', '').strip()
        advice    = request.form.get('advice', '').strip()

        visit = Visit(
            patient_id=patient_id,
            complaint=complaint,
            diagnosis=diagnosis,
            bp=bp, weight=weight, advice=advice,
            visit_date=datetime.utcnow()
        )
        db.session.add(visit)
        db.session.flush()

        medicine_names = request.form.getlist('medicine_name[]')
        dosages        = request.form.getlist('dosage[]')
        days_list      = request.form.getlist('days[]')
        instructions   = request.form.getlist('instructions[]')

        for i, med_name in enumerate(medicine_names):
            med_name = med_name.strip()
            if not med_name:
                continue
            days_val = None
            if i < len(days_list) and days_list[i].strip().isdigit():
                days_val = int(days_list[i].strip())

            rx = Prescription(
                visit_id=visit.id,
                medicine_name=med_name,
                dosage=dosages[i].strip() if i < len(dosages) else '',
                days=days_val,
                start_date=date.today(),
                instructions=instructions[i].strip() if i < len(instructions) else ''
            )
            db.session.add(rx)

        # Optional bill for follow-up
        try:
            consultation_fee = float(request.form.get('consultation_fee', 0) or 0)
            medicine_cost    = float(request.form.get('medicine_cost', 0) or 0)
            other_charges    = float(request.form.get('other_charges', 0) or 0)
        except ValueError:
            consultation_fee = medicine_cost = other_charges = 0.0

        total = consultation_fee + medicine_cost + other_charges
        if total > 0:
            bill = Bill(
                patient_id=patient_id,
                visit_id=visit.id,
                consultation_fee=consultation_fee,
                medicine_cost=medicine_cost,
                other_charges=other_charges,
                total=total,
                notes=request.form.get('bill_notes', '').strip()
            )
            db.session.add(bill)

        try:
            db.session.commit()
            flash(f'Visit recorded for {patient.name}!', 'success')
            return redirect(url_for('patient_detail', patient_id=patient.id))
        except Exception:
            db.session.rollback()
            flash('Error saving visit. Please try again.', 'danger')

    return render_template('add_visit.html', patient=patient, clinic_name=CLINIC_NAME)


@app.route('/visits/<int:visit_id>')
@login_required
def visit_detail(visit_id):
    visit   = Visit.query.get_or_404(visit_id)
    patient = visit.patient
    return render_template('visit_detail.html', visit=visit, patient=patient, clinic_name=CLINIC_NAME)


@app.route('/visits/<int:visit_id>/delete', methods=['POST'])
@login_required
def delete_visit(visit_id):
    visit = Visit.query.get_or_404(visit_id)
    patient_id = visit.patient_id
    try:
        db.session.delete(visit)
        db.session.commit()
        flash('Visit deleted.', 'warning')
    except Exception:
        db.session.rollback()
        flash('Could not delete visit.', 'danger')
    return redirect(url_for('patient_detail', patient_id=patient_id))


# ─── Billing Routes ───────────────────────────────────────────────────────────

@app.route('/patients/<int:patient_id>/bills/add', methods=['GET', 'POST'])
@login_required
def add_bill(patient_id):
    patient = Patient.query.get_or_404(patient_id)
    visits  = Visit.query.filter_by(patient_id=patient_id)\
                         .order_by(Visit.visit_date.desc()).all()

    if request.method == 'POST':
        try:
            consultation_fee = float(request.form.get('consultation_fee', 0) or 0)
            medicine_cost    = float(request.form.get('medicine_cost', 0) or 0)
            other_charges    = float(request.form.get('other_charges', 0) or 0)
            total            = consultation_fee + medicine_cost + other_charges
            visit_id_str     = request.form.get('visit_id', '').strip()
            visit_id         = int(visit_id_str) if visit_id_str.isdigit() else None
            notes            = request.form.get('notes', '').strip()

            bill = Bill(
                patient_id=patient_id, visit_id=visit_id,
                consultation_fee=consultation_fee,
                medicine_cost=medicine_cost,
                other_charges=other_charges,
                total=total, notes=notes
            )
            db.session.add(bill)
            db.session.commit()
            flash(f'Bill of ₹{total:.2f} created for {patient.name}.', 'success')
            return redirect(url_for('patient_detail', patient_id=patient_id))
        except Exception:
            db.session.rollback()
            flash('Error creating bill.', 'danger')

    return render_template('add_bill.html', patient=patient, visits=visits, clinic_name=CLINIC_NAME)


# ─── PDF Prescription ──────────────────────────────────────────────────────────

def _build_prescription_pdf(buffer, patient, visit):
    """Shared PDF builder for prescription."""
    doc = SimpleDocTemplate(buffer, pagesize=A4,
                            rightMargin=2*cm, leftMargin=2*cm,
                            topMargin=2*cm, bottomMargin=2*cm)
    elements = []

    clinic_style  = ParagraphStyle('clinic',  fontSize=18, fontName='Helvetica-Bold',
                                   alignment=TA_CENTER, textColor=colors.HexColor('#1a5276'), spaceAfter=4)
    doctor_style  = ParagraphStyle('doctor',  fontSize=11, fontName='Helvetica-Bold',
                                   alignment=TA_CENTER, textColor=colors.HexColor('#2e86c1'), spaceAfter=2)
    address_style = ParagraphStyle('address', fontSize=9,  fontName='Helvetica',
                                   alignment=TA_CENTER, textColor=colors.grey, spaceAfter=2)
    section_style = ParagraphStyle('section', fontSize=10, fontName='Helvetica-Bold',
                                   textColor=colors.HexColor('#1a5276'), spaceBefore=10, spaceAfter=4)
    body_style    = ParagraphStyle('body',    fontSize=10, fontName='Helvetica',
                                   textColor=colors.HexColor('#2c3e50'), spaceAfter=6, leading=16)
    label_style   = ParagraphStyle('label',   fontSize=9,  fontName='Helvetica-Bold',
                                   textColor=colors.HexColor('#555555'))

    elements.append(Paragraph(CLINIC_NAME, clinic_style))
    elements.append(Paragraph(DOCTOR_NAME, doctor_style))
    elements.append(Paragraph(DOCTOR_DEGREE, address_style))
    elements.append(Paragraph(f'{CLINIC_ADDRESS} | {CLINIC_PHONE}', address_style))
    elements.append(Spacer(1, 0.3*cm))
    elements.append(HRFlowable(width='100%', thickness=2, color=colors.HexColor('#1a5276')))
    elements.append(Spacer(1, 0.2*cm))

    visit_date_str = visit.visit_date.strftime('%d %B %Y') if visit else (
        patient.created_at.strftime('%d %B %Y') if patient.created_at else '')
    bp_val     = (visit.bp     if visit else patient.bp)     or '—'
    weight_val = (visit.weight if visit else patient.weight) or '—'

    info_data = [
        [Paragraph('<b>Serial No:</b>', label_style), Paragraph(patient.serial_number, body_style),
         Paragraph('<b>Reg. No:</b>',   label_style), Paragraph(patient.registration_number, body_style)],
        [Paragraph('<b>Date:</b>',      label_style), Paragraph(visit_date_str, body_style),
         Paragraph('<b>Code:</b>',      label_style), Paragraph(patient.code or '—', body_style)],
        [Paragraph('<b>Patient Name:</b>', label_style), Paragraph(patient.name, body_style),
         Paragraph('<b>Age / Sex:</b>',   label_style), Paragraph(f"{patient.age} Yrs / {patient.sex}", body_style)],
        [Paragraph('<b>Mobile:</b>',    label_style), Paragraph(patient.mobile, body_style),
         Paragraph('<b>BP / Weight:</b>', label_style), Paragraph(f"{bp_val} / {weight_val}", body_style)],
        [Paragraph('<b>Address:</b>',   label_style), Paragraph(patient.address or '—', body_style), '', ''],
    ]
    info_table = Table(info_data, colWidths=[3*cm, 6.5*cm, 3*cm, 5*cm])
    info_table.setStyle(TableStyle([
        ('ROWBACKGROUNDS', (0, 0), (-1, -1), [colors.HexColor('#f4f9fd'), colors.white]),
        ('GRID',    (0, 0), (-1, -1), 0.3, colors.HexColor('#d5e8f5')),
        ('VALIGN',  (0, 0), (-1, -1), 'TOP'),
        ('PADDING', (0, 0), (-1, -1), 6),
        ('SPAN', (1, 4), (3, 4)),
    ]))
    elements.append(info_table)
    elements.append(Spacer(1, 0.4*cm))

    complaint_text = (visit.complaint if visit else patient.complaint)
    diagnosis_text = visit.diagnosis if visit else None

    if complaint_text:
        elements.append(Paragraph('Chief Complaint', section_style))
        elements.append(HRFlowable(width='100%', thickness=0.5, color=colors.HexColor('#aed6f1')))
        elements.append(Spacer(1, 0.2*cm))
        elements.append(Paragraph(complaint_text, body_style))

    if diagnosis_text:
        elements.append(Paragraph('Diagnosis', section_style))
        elements.append(HRFlowable(width='100%', thickness=0.5, color=colors.HexColor('#aed6f1')))
        elements.append(Spacer(1, 0.2*cm))
        elements.append(Paragraph(diagnosis_text, body_style))

    elements.append(Paragraph('Rx — Prescription', section_style))
    elements.append(HRFlowable(width='100%', thickness=0.5, color=colors.HexColor('#aed6f1')))
    elements.append(Spacer(1, 0.2*cm))

    if visit and visit.prescriptions:
        rx_data = [[
            Paragraph('<b>#</b>', label_style),
            Paragraph('<b>Medicine</b>', label_style),
            Paragraph('<b>Dosage</b>', label_style),
            Paragraph('<b>Duration</b>', label_style),
            Paragraph('<b>Instructions</b>', label_style),
        ]]
        for idx, rx in enumerate(visit.prescriptions, 1):
            rx_data.append([
                Paragraph(str(idx), body_style),
                Paragraph(rx.medicine_name, body_style),
                Paragraph(rx.dosage or '—', body_style),
                Paragraph(f'{rx.days} days' if rx.days else '—', body_style),
                Paragraph(rx.instructions or '—', body_style),
            ])
        rx_table = Table(rx_data, colWidths=[0.8*cm, 5.5*cm, 2.5*cm, 2.5*cm, 6.2*cm])
        rx_table.setStyle(TableStyle([
            ('BACKGROUND',     (0, 0), (-1, 0),  colors.HexColor('#e8f3fb')),
            ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor('#f9fdff')]),
            ('GRID',           (0, 0), (-1, -1), 0.3, colors.HexColor('#d5e8f5')),
            ('VALIGN',         (0, 0), (-1, -1), 'TOP'),
            ('PADDING',        (0, 0), (-1, -1), 5),
        ]))
        elements.append(rx_table)
    elif patient.medicine:
        for line in patient.medicine.strip().split('\n'):
            if line.strip():
                elements.append(Paragraph(f'• {line.strip()}', body_style))
    else:
        elements.append(Paragraph('—', body_style))

    advice_text = (visit.advice if visit else patient.advice)
    if advice_text:
        elements.append(Paragraph('Advice & Instructions', section_style))
        elements.append(HRFlowable(width='100%', thickness=0.5, color=colors.HexColor('#aed6f1')))
        elements.append(Spacer(1, 0.2*cm))
        for line in advice_text.strip().split('\n'):
            if line.strip():
                elements.append(Paragraph(f'• {line.strip()}', body_style))

    elements.append(Spacer(1, 2*cm))
    elements.append(HRFlowable(width='40%', thickness=0.5, color=colors.grey, hAlign='RIGHT'))
    elements.append(Paragraph(f'{DOCTOR_NAME}<br/>{DOCTOR_DEGREE}',
                               ParagraphStyle('sig', fontSize=10, alignment=TA_RIGHT,
                                              textColor=colors.HexColor('#2e86c1'))))
    elements.append(Spacer(1, 0.5*cm))
    elements.append(HRFlowable(width='100%', thickness=1, color=colors.HexColor('#d5e8f5')))
    elements.append(Paragraph('This is a computer-generated prescription.',
                               ParagraphStyle('footer', fontSize=8, alignment=TA_CENTER,
                                              textColor=colors.grey, spaceBefore=4)))
    doc.build(elements)


@app.route('/patients/<int:patient_id>/prescription')
@login_required
def prescription_pdf(patient_id):
    patient      = Patient.query.get_or_404(patient_id)
    latest_visit = Visit.query.filter_by(patient_id=patient_id)\
                              .order_by(Visit.visit_date.desc()).first()
    buffer = io.BytesIO()
    _build_prescription_pdf(buffer, patient, latest_visit)
    buffer.seek(0)
    response = make_response(buffer.read())
    response.headers['Content-Type'] = 'application/pdf'
    response.headers['Content-Disposition'] = \
        f'inline; filename=prescription_{patient.serial_number}.pdf'
    return response


@app.route('/visits/<int:visit_id>/prescription')
@login_required
def visit_prescription_pdf(visit_id):
    visit   = Visit.query.get_or_404(visit_id)
    patient = visit.patient
    buffer  = io.BytesIO()
    _build_prescription_pdf(buffer, patient, visit)
    buffer.seek(0)
    response = make_response(buffer.read())
    response.headers['Content-Type'] = 'application/pdf'
    response.headers['Content-Disposition'] = \
        f'inline; filename=rx_visit_{visit_id}.pdf'
    return response


# ─── Manual WhatsApp Test Route ───────────────────────────────────────────────

@app.route('/patients/<int:patient_id>/send-reminder', methods=['POST'])
@login_required
def send_manual_reminder(patient_id):
    """Doctor can manually trigger a WhatsApp reminder for a patient."""
    patient = Patient.query.get_or_404(patient_id)
    message = (
        f"Namaskar {patient.name} ji 🙏\n\n"
        f"Yeh aapke doctor {DOCTOR_NAME} ki taraf se ek reminder hai.\n"
        f"Kripya apni dawai samay par lein aur follow-up ke liye clinic mein aayein.\n\n"
        f"— {CLINIC_NAME}\n📞 {CLINIC_PHONE}"
    )
    success = send_whatsapp(patient.mobile, message)
    if success:
        flash(f'WhatsApp reminder sent to {patient.name} ({patient.mobile})!', 'success')
    else:
        flash('Could not send WhatsApp message. Check Twilio credentials in .env', 'danger')
    return redirect(url_for('patient_detail', patient_id=patient_id))


# ─── Init DB ──────────────────────────────────────────────────────────────────

def init_db():
    with app.app_context():
        db.create_all()
        if not User.query.first():
            admin = User(username='doctor', name=DOCTOR_NAME)
            admin.set_password('clinic@123')
            db.session.add(admin)
            db.session.commit()
            print("✅ Default user: doctor | password: clinic@123")


# ─── Run ──────────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    init_db()
    app.run(debug=os.environ.get('FLASK_DEBUG', 'True') == 'True', host='0.0.0.0', port=5000)