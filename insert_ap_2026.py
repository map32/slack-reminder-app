import os
import csv
import io
from datetime import datetime
from sqlalchemy import create_engine, Column, Integer, String, Date, ForeignKey, UniqueConstraint
from sqlalchemy.orm import declarative_base, sessionmaker, relationship

# -------------------------
# 1. Configuration
# -------------------------
# CHANGE THIS to your actual connection string
DATABASE_URL = os.environ.get("DATABASE_URL")

Base = declarative_base()

# -------------------------
# 2. Database Models
#    (Must match app.py exactly)
# -------------------------
class EventType(Base):
    __tablename__ = 'event_type'
    name = Column(String(50), primary_key=True)

class AppAdmin(Base):
    __tablename__ = 'app_admin'
    user_slack_id = Column(String(50), primary_key=True)

class Event(Base):
    __tablename__ = 'event'
    id = Column(Integer, primary_key=True)
    title = Column(String(200), nullable=False)
    event_type = Column(String(50), nullable=False)
    event_date = Column(Date, nullable=False)
    registration_deadline = Column(Date, nullable=False)

class Subscription(Base):
    __tablename__ = 'subscription'
    id = Column(Integer, primary_key=True)
    user_slack_id = Column(String(50), nullable=False)
    event_id = Column(Integer, ForeignKey('event.id'), nullable=False)
    __table_args__ = (UniqueConstraint('user_slack_id', 'event_id', name='_user_event_uc'),)

# -------------------------
# 3. The Raw Data
# -------------------------
CSV_DATA = """Exam Name,Window,Format,Location,Date,Status
Research,Standard,Portfolio,Online Submission,2026-04-30,Open
Biology,Standard,Hybrid Digital,"KAEC, Seoul",2026-05-04,Limited seats
Latin,Standard,Fully Digital,"KAEC, Seoul",2026-05-04,Open
Microeconomics,Standard,Hybrid Digital,"KAEC, Seoul",2026-05-04,Limited seats
European History,Standard,Fully Digital,"KAEC, Seoul",2026-05-04,Open
European History,Standard,Fully Digital,"Hotel Sirius, Jeju",2026-05-04,Full
Chemistry,Standard,Hybrid Digital,"KAEC, Seoul",2026-05-05,Full
Human Geography,Standard,Fully Digital,"KAEC, Seoul",2026-05-05,Open
Human Geography,Standard,Fully Digital,"Hotel Sirius, Jeju",2026-05-05,Open
United States Government and Politics,Standard,Fully Digital,"KAEC, Seoul",2026-05-05,Open
United States Government and Politics,Standard,Fully Digital,"Hotel Sirius, Jeju",2026-05-05,Open
English Literature and Composition,Standard,Fully Digital,"KAEC, Seoul",2026-05-06,Open
English Literature and Composition,Standard,Fully Digital,"Hotel Sirius, Jeju",2026-05-06,Open
Comparative Government and Politics,Standard,Fully Digital,"KAEC, Seoul",2026-05-06,Open
Comparative Government and Politics,Standard,Fully Digital,"Hotel Sirius, Jeju",2026-05-06,Open
Physics 1 Algebra-Based,Standard,Hybrid Digital,"KAEC, Seoul",2026-05-06,Open
World History Modern,Standard,Fully Digital,"KAEC, Seoul",2026-05-07,Open
World History Modern,Standard,Fully Digital,"Hotel Sirius, Jeju",2026-05-07,Open
Physics 2 Algebra-Based,Standard,Hybrid Digital,"KAEC, Seoul",2026-05-07,Open
Statistics,Standard,Hybrid Digital,"KAEC, Seoul",2026-05-07,Open
African American Studies,Standard,Fully Digital,"KAEC, Seoul",2026-05-07,Open
United States History,Standard,Fully Digital,"KAEC, Seoul",2026-05-08,Open
United States History,Standard,Fully Digital,"Hotel Sirius, Jeju",2026-05-08,Open
Italian Language and Culture,Standard,PBT,"KAEC, Seoul",2026-05-08,Open
Macroeconomics,Standard,Hybrid Digital,"KAEC, Seoul",2026-05-08,Limited seats
Chinese Language and Culture,Standard,CBT,"KAEC, Seoul",2026-05-08,Open
Drawing,Standard,Portfolio,Online Submission,2026-05-08,Open
2-D Art and Design,Standard,Portfolio,Online Submission,2026-05-08,Open
3-D Art and Design,Standard,Portfolio,Online Submission,2026-05-08,Open
Calculus AB,Standard,Hybrid Digital,"KAEC, Seoul",2026-05-11,Limited seats
Calculus AB,Standard,Hybrid Digital,"Garden Hotel, Seoul",2026-05-11,Full
Calculus BC,Standard,Hybrid Digital,"KAEC, Seoul",2026-05-11,Full
Calculus BC,Standard,Hybrid Digital,"Garden Hotel, Seoul",2026-05-11,Limited seats
Music Theory,Standard,PBT,"KAEC, Seoul",2026-05-11,Limited seats
Seminar,Standard,Portfolio+Fully Digital,"KAEC, Seoul",2026-05-11,Limited seats
French Language and Culture,Standard,PBT,"KAEC, Seoul",2026-05-12,Open
Precalculus,Standard,Hybrid Digital,"KAEC, Seoul",2026-05-12,Open
Psychology,Standard,Fully Digital,"KAEC, Seoul",2026-05-12,Open
Psychology,Standard,Fully Digital,"Hotel Sirius, Jeju",2026-05-12,Open
Japanese Language and Culture,Standard,CBT,"KAEC, Seoul",2026-05-12,Open
English Language and Composition,Standard,Fully Digital,"KAEC, Seoul",2026-05-13,Open
English Language and Composition,Standard,Fully Digital,"Hotel Sirius, Jeju",2026-05-13,Limited seats
German Language and Culture,Standard,PBT,"KAEC, Seoul",2026-05-13,Open
Physics C Mechanics,Standard,Hybrid Digital,"KAEC, Seoul",2026-05-13,Open
Spanish Literature and Culture,Standard,PBT,"KAEC, Seoul",2026-05-13,Open
Art History,Standard,Fully Digital,"KAEC, Seoul",2026-05-14,Open
Art History,Standard,Fully Digital,"Hotel Sirius, Jeju",2026-05-14,Open
Spanish Language and Culture,Standard,PBT,"KAEC, Seoul",2026-05-14,Limited seats
Physics C Electricity and Magnetism,Standard,Hybrid Digital,"KAEC, Seoul",2026-05-14,Limited seats
Computer Science Principles,Standard,Portfolio+Fully Digital,"KAEC, Seoul",2026-05-14,Open
Computer Science Principles,Standard,Portfolio+Fully Digital,"Hotel Sirius, Jeju",2026-05-14,Open
Environmental Science,Standard,Fully Digital,"KAEC, Seoul",2026-05-15,Open
Environmental Science,Standard,Fully Digital,"Hotel Sirius, Jeju",2026-05-15,Open
Computer Science A,Standard,Fully Digital,"KAEC, Seoul",2026-05-15,Open
Computer Science A,Standard,Fully Digital,"Hotel Sirius, Jeju",2026-05-15,Open
European History,Late,Fully Digital,"KAEC, Seoul",2026-05-18,Open
Comparative Government and Politics,Late,Fully Digital,"KAEC, Seoul",2026-05-18,Open
World History Modern,Late,Fully Digital,"KAEC, Seoul",2026-05-18,Open
English Literature and Composition,Late,Fully Digital,"KAEC, Seoul",2026-05-18,Open
Human Geography,Late,Fully Digital,"KAEC, Seoul",2026-05-18,Open
United States Government and Politics,Late,Fully Digital,"KAEC, Seoul",2026-05-19,Open
Japanese Language and Culture,Late,CBT,"KAEC, Seoul",2026-05-19,Open
United States History,Late,Fully Digital,"KAEC, Seoul",2026-05-19,Open
African American Studies,Late,Fully Digital,"KAEC, Seoul",2026-05-19,Open
Microeconomics,Late,Hybrid Digital,"KAEC, Seoul",2026-05-20,Open
Statistics,Late,Hybrid Digital,"KAEC, Seoul",2026-05-20,Open
Biology,Late,Hybrid Digital,"KAEC, Seoul",2026-05-20,Open
Chemistry,Late,Hybrid Digital,"KAEC, Seoul",2026-05-20,Open
Macroeconomics,Late,Hybrid Digital,"KAEC, Seoul",2026-05-20,Open
English Language and Composition,Late,Fully Digital,"KAEC, Seoul",2026-05-21,Open
Chinese Language and Culture,Late,CBT,"KAEC, Seoul",2026-05-21,Limited seats
Computer Science Principles,Late,Portfolio+Fully Digital,"KAEC, Seoul",2026-05-21,Open
Precalculus,Late,Hybrid Digital,"KAEC, Seoul",2026-05-21,Open
Calculus AB,Late,Hybrid Digital,"KAEC, Seoul",2026-05-21,Limited seats
Calculus BC,Late,Hybrid Digital,"KAEC, Seoul",2026-05-21,Full
Physics C Mechanics,Late,Hybrid Digital,"KAEC, Seoul",2026-05-21,Open
Physics 2 Algebra-Based,Late,Hybrid Digital,"KAEC, Seoul",2026-05-21,Limited seats
Environmental Science,Late,Fully Digital,"KAEC, Seoul",2026-05-22,Open
Physics 1 Algebra-Based,Late,Hybrid Digital,"KAEC, Seoul",2026-05-22,Open
Computer Science A,Late,Fully Digital,"KAEC, Seoul",2026-05-22,Open
Psychology,Late,Fully Digital,"KAEC, Seoul",2026-05-22,Open
Physics C Electricity and Magnetism,Late,Hybrid Digital,"KAEC, Seoul",2026-05-22,Open
"""

# -------------------------
# 4. Initialization Logic
# -------------------------
def main():
    print(f"Connecting to database: {DATABASE_URL}")
    engine = create_engine(DATABASE_URL)
    Session = sessionmaker(bind=engine)
    session = Session()

    # A. Create Tables
    print("Creating tables if they do not exist...")
    Base.metadata.create_all(engine)

    # B. Seed Categories
    # We add standard categories like AP, SAT, ACT so the app isn't empty
    default_types = ["AP", "SAT", "ACT", "GCSE", "Extracurricular"]
    for t_name in default_types:
        if not session.query(EventType).filter_by(name=t_name).first():
            print(f"Adding Category: {t_name}")
            session.add(EventType(name=t_name))
    session.commit()

    # C. Seed Events from CSV
    print("Parsing and inserting event data...")
    deadline = datetime.strptime("2026-03-10", "%Y-%m-%d").date()

    reader = csv.DictReader(io.StringIO(CSV_DATA))
    events_to_add = []

    for row in reader:
        # Construct Title
        status_indicator = ""
        if row['Status'] == 'Full':
            status_indicator = " (FULL)"
        elif row['Status'] == 'Limited seats':
            status_indicator = " (Low Seats)"

        formatted_title = f"{row['Exam Name']} [{row['Window']}, {row['Location']}]{status_indicator}"
        
        try:
            event_date_obj = datetime.strptime(row['Date'], "%Y-%m-%d").date()
            
            new_event = Event(
                title=formatted_title,
                event_type="AP", # Assuming everything in this CSV is an AP exam
                event_date=event_date_obj,
                registration_deadline=deadline
            )
            events_to_add.append(new_event)
        except ValueError as e:
            print(f"Skipping row due to date error: {row['Exam Name']} - {e}")

    if events_to_add:
        session.add_all(events_to_add)
        session.commit()
        print(f"Successfully inserted {len(events_to_add)} events.")
    else:
        print("No valid events found in CSV data.")

    session.close()
    print("Database initialization complete.")

if __name__ == "__main__":
    main()