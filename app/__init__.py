"""Initialize Stair Challenge app."""
# ruff: noqa: E402, ARG001
# pylint: disable=wrong-import-position, ungrouped-imports, import-outside-toplevel, global-statement
from __future__ import annotations

import getpass
import json
import os
import threading
from datetime import datetime, timedelta

import pytz
import sqlalchemy as sqla
from flask import Flask, redirect, request, session, url_for
from flask_login import LoginManager
from flask_socketio import SocketIO
from flask_sqlalchemy import SQLAlchemy
from rpi_ws281x import Color
from sqlalchemy import exc
from sqlalchemy.exc import SQLAlchemyError

from app.const import (
    MQTT_RESTART_ALL_TOPIC,
    MQTT_SENSOR,
    MQTT_STATUS_TOPIC,
    MQTT_TRIGGER_TOPIC,
    MQTT_WORKOUT,
    MQTT_WORKOUT_CONTROL_ALL_TOPIC,
    SENSOR_LOCATION,
    WORKOUT_SETTINGS,
    WORKOUTS,
    IsAdmin,
    ResetCounter,
)
from app.led_controller import Colors, LEDController
from app.mqtt_controller import MQTTClient

db = SQLAlchemy()
mqtt = MQTTClient()
socketio = SocketIO(engineio_logger=False, logger=False, cors_allowed_origins="*")
login = LoginManager()
login.login_view = "auth.login"

first_trigger: bool = True
workout_id: int = None

last_triggered_client_id: int = None
client_counters: list = [1]
stair_counter: int = 0
steps_counter: int = 0

sandglass_thread: threading.Thread = None
christmas_colors: list = [Colors.RED, Colors.GREEN]


# ----------------------------------------------------------------------------#
# LED strip configuration.
# ----------------------------------------------------------------------------#
LED_COUNT = 104  # Number of LED pixels.
LED_PIN = 10  # GPIO pin connected to the pixels (18 uses PWM!).
LED_FREQ_HZ = 800000  # LED signal frequency in hertz (usually 800khz)
LED_DMA = 10  # DMA channel to use for generating signal (try 10)
LED_BRIGHTNESS = 125  # Set to 0 for darkest and 255 for brightest
LED_INVERT = False  # ON to invert the signal (Using level shift)
LED_CHANNEL = 0  # set to '1' for GPIOs 13, 19, 41, 45 or 53

led_controller = LEDController(
    LED_COUNT,
    LED_PIN,
    LED_FREQ_HZ,
    LED_DMA,
    LED_BRIGHTNESS,
    LED_INVERT,
    LED_CHANNEL,
)

# -----------------------------------
# Create Application Factory Function
# -----------------------------------


def create_app() -> Flask:
    """Create the Flask application.

    Returns
    -------
        Flask: The Flask application.
    """
    app = Flask(__name__)

    # Load config values from app/config.py
    if os.getenv("FLASK_ENV") == "production":
        app.config.from_object("config.ProductionConfig")
    elif os.getenv("FLASK_ENV") == "testing":
        app.config.from_object("config.TestingConfig")
    else:
        app.config.from_object("config.DevelopmentConfig")

    # Initialize the socketio instance
    socketio.init_app(app)
    socketio.async_mode = app.config["SOCKETIO_ASYNC_MODE"]
    led_controller.start()
    led_controller.turn_off()

    initialize_extensions(app)
    register_blueprints(app)
    register_cli_commands(app)
    register_routes(app)
    register_mqtt_events(app)

    # Check if the database needs to be initialized
    engine = sqla.create_engine(app.config["SQLALCHEMY_DATABASE_URI"])
    inspector = sqla.inspect(engine)
    if not inspector.has_table("users"):
        with app.app_context():
            db.drop_all()
            db.create_all()
            app.logger.info("Initialized the database!")
    else:
        app.logger.info("Database already contains the users table.")

    # Show visual feedback that the app is ready
    led_controller.rainbow()
    led_controller.color_wipe(Color(0, 0, 0), 10)

    return app


def initialize_extensions(app: Flask) -> None:
    """Initialize the extensions.

    Args:
    ----
        app: The Flask application.
    """
    # Initialize the database
    db.init_app(app)

    # Initialize MQTT client
    mqtt.connect(
        app.config["MQTT_BROKER_URL"],
        app.config["MQTT_BROKER_PORT"],
        app.config["MQTT_KEEPALIVE"],
    )

    # Initialize the login manager
    login.init_app(app)

    # The user_loader decorator allows flask-login to load the current user
    # and grab their id.
    @login.user_loader
    def load_user(user_id: int) -> User:
        """Load the current user.

        Args:
        ----
            user_id (int): The user id to load.

        Returns:
        -------
            User: The current user.
        """
        return db.session.get(User, user_id)

    @app.before_request
    def make_session_permanent() -> None:
        """Make the session permanent."""
        session.permanent: bool = True
        app.permanent_session_lifetime = timedelta(hours=12)


from app.blueprints.auth.models import User
from app.blueprints.backend.models import Sensor, Workout


def register_blueprints(app: Flask) -> None:
    """Register the blueprints.

    Args:
    ----
        app: The Flask application.
    """
    from app.blueprints.auth import bp as auth_bp
    from app.blueprints.backend import bp as backend_bp
    from app.blueprints.frontend import bp as frontend_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(backend_bp, url_prefix="/admin")
    app.register_blueprint(frontend_bp)


def register_cli_commands(app: Flask) -> None:
    """Register the CLI commands.

    Args:
    ----
        app: The Flask application.
    """

    @app.cli.command("init_db")
    def init_db() -> None:
        """Initialize the database."""
        db.drop_all()
        db.create_all()
        print("Database initialized successfully!")

    @app.cli.command("seed_workouts")
    def seed_workouts() -> None:
        """Seed the workouts table."""
        for index, workout in enumerate(WORKOUTS, 1):
            db.session.add(
                Workout(
                    name=workout["name"],
                    description=workout["description"],
                    pros=workout["pros"],
                    cons=None if "cons" not in workout else workout["cons"],
                ),
            )
            db.session.commit()
            print(f"Workout {index} added successfully!")
        print("Workouts table seeded successfully!")

    @app.cli.command("create_admin")
    def create_admin() -> None:
        """Create a new admin user."""
        name = input("Enter name: ")
        email = input("Enter email address: ")
        password = getpass.getpass("Enter password: ")
        confirm_password = getpass.getpass("Enter password again: ")

        # Validate the password
        if password != confirm_password:
            print("Passwords don't match")
            return

        # Create the user
        try:
            user = User(
                name=name,
                email=email,
                password=password,
                is_admin=IsAdmin.YES.value,
                created_at=datetime.now(pytz.timezone("Europe/Amsterdam")),
            )
            db.session.add(user)
            db.session.commit()
            print(f"Admin with email {email} created successfully!")
        except SQLAlchemyError as error:
            print(f"Could not create admin: {error}")
            db.session.rollback()
            return


def control_workout(event: dict) -> None:
    """General function to control the workouts.

    Args:
    ----
        event (dict): The event data.
    """
    global WORKOUT_SETTINGS, workout_id, first_trigger

    if event["mode"] == "start":
        print(f"Starting workout - nr: {event['workout_id']}")
        workout_id = event["workout_id"]

        WORKOUT_SETTINGS = {
            "active": True,
            "color": event.get("color", None),
            "christmas_mode": event.get("christmas_mode", False),
        }

        if workout_id == 2:
            first_trigger = True
            handle_workout_id_2(event, start=True)
        else:
            mqtt.send(MQTT_WORKOUT_CONTROL_ALL_TOPIC, "start")

    elif event["mode"] == "stop" or event["mode"] == "finished":
        print(f"Stopping workout - nr: {event['workout_id']}")
        mqtt.send(MQTT_WORKOUT_CONTROL_ALL_TOPIC, "stop")

        WORKOUT_SETTINGS = {
            "active": False,
            "color": None,
            "christmas_mode": False,
        }

        if workout_id == 2:
            handle_workout_id_2(event, start=False)

        workout_id = None
        led_controller.color_wipe(Color(0, 0, 0), 10)


def handle_workout_id_2(event: dict, start: bool) -> None:  # noqa: FBT001
    """Handle the workout with ID 2.

    Args:
    ----
        event (dict): The event data.
        start (bool): True if the workout should start, False if not.
    """
    global last_triggered_client_id

    end_sensor = int(event["end_sensor"][7:])

    if start:
        if end_sensor not in client_counters:
            client_counters.append(end_sensor)

        activate_specific_sensors()
        start_workout_2_thread(event)
    else:
        client_counters.remove(end_sensor)
        last_triggered_client_id = None

        stop_workout_2_thread(event)

        if event["mode"] == "finished" and event["led_toggle"]:
            led_controller.rainbow()
            led_controller.color_wipe(Color(0, 0, 0), 10)


def activate_specific_sensors() -> None:
    """Activate the sensors via MQTT."""
    for client in client_counters:
        print(f"Activate sensor: {client}")
        mqtt.send(f"{MQTT_WORKOUT}/{client}/control", "start")


def start_workout_2_thread(event: dict) -> None:
    """Start the sandglass thread.

    Args:
    ----
        event (dict): The event data.
    """
    global sandglass_thread
    colors = Colors()

    if sandglass_thread is not None and sandglass_thread.is_alive():
        led_controller.stop_sandglass_thread()
        sandglass_thread.join()
        sandglass_thread = None

    if event["led_toggle"]:
        sandglass_thread = threading.Thread(
            target=led_controller.sandglass,
            args=(event["time"], colors.hex_to_rgb(event["color"])),
        )
        sandglass_thread.start()
    else:
        led_controller.set_sensor_led(colors.BLUE, int(event["end_sensor"][7:]))
        led_controller.one_led(colors.GREEN, 103)
        update_counters(0, ResetCounter.YES)


def stop_workout_2_thread(event: dict) -> None:
    """Stop the sandglass thread.

    Args:
    ----
        event (dict): The event data.
    """
    global sandglass_thread
    colors = Colors()

    if event["led_toggle"]:
        led_controller.stop_sandglass_thread()
        sandglass_thread = None
        print(f"thread stopped: {led_controller.stop_sandglass_thread()}")
    else:
        led_controller.one_led(colors.RED, 103)


def workout_counting(client_id: int) -> None:
    """Start the counter for the workout.

    Args:
    ----
        client_id: The client ID of the sensor that triggered.
    """
    global last_triggered_client_id, first_trigger

    # print(client_counters)
    if client_id in client_counters and client_id != last_triggered_client_id:
        if first_trigger:
            first_trigger = False
        else:
            update_counters(1)
        last_triggered_client_id = client_id


def update_counters(value: int, reset: ResetCounter = ResetCounter.NO) -> None:
    """Update the counter on the frontend.

    Args:
    ----
        value: The value to update the counter with.
        reset (ResetCounter): Reset the counter.
    """
    global stair_counter, steps_counter
    if reset == ResetCounter.YES:
        # reset the counter
        stair_counter = 0
        steps_counter = 0
    else:
        # Update the stair counter
        stair_counter += value

        # Update the steps counter
        steps = {
            1: 1,
            2: 3,
            3: 5,
            4: 7,
            5: 9,
            6: 11,
        }.get(client_counters[1])
        steps_counter += steps

        print(f"Stair counter: {stair_counter}, Steps counter: {steps_counter}")
    # Send the counter value to the frontend
    socketio.emit(
        "counter",
        {"stair_counter": stair_counter, "steps_counter": steps_counter},
    )


def register_mqtt_events(app: Flask) -> None:
    """Register the MQTT events.

    Args:
    ----
        app: The Flask application.
    """

    def is_client_id_valid(client_id: str) -> bool:
        """Run check on valid sensor in database.

        Args:
        ----
            client_id (str): The client ID to check.

        Returns:
        -------
            bool: True if the client ID is valid, False if not.
        """
        with app.app_context():
            sensor = Sensor.query.filter_by(client_id=f"sensor-{client_id}").first()
        return sensor is not None

    def on_topic_trigger(
        client: MQTTClient,  # pylint: disable=unused-argument
        userdata: dict,  # pylint: disable=unused-argument
        message: dict,
    ) -> None:
        """MQTT function to handle trigger messages.

        Args:
        ----
            client: The client instance for this callback.
            userdata: The private user data as set in Client() or userdata_set().
            message: An instance of MQTTMessage.
        """
        colors = Colors()
        data: dict = json.loads(message.payload)
        client_id = data["client_id"]

        if WORKOUT_SETTINGS["active"] and is_client_id_valid(client_id):
            match workout_id:
                case 1:
                    # Kameleon
                    led_controller.set_color(colors.get_random_unique_color())
                case 2:
                    # Trap op, trap af
                    workout_counting(client_id)
                case 3:
                    # Meeloper
                    print("Workout 3")
                case 4:
                    # Waterdruppels
                    # Define the color effect
                    if WORKOUT_SETTINGS["christmas_mode"]:
                        christmas_colors[0], christmas_colors[1] = (
                            christmas_colors[1],
                            christmas_colors[0],
                        )
                        color_effect = christmas_colors[1]
                    else:
                        color_effect = colors.hex_to_rgb(WORKOUT_SETTINGS["color"])

                    # Start the thread
                    thread = threading.Thread(
                        target=led_controller.ripple_effect,
                        args=(
                            SENSOR_LOCATION.get(client_id),
                            12,
                            color_effect,
                        ),
                    )
                    thread.start()
                case _:
                    print("Workout not found")
        print(f"Message Received from Others: {message.payload.decode()}")

    def on_topic_status(
        client: MQTTClient,  # pylint: disable=unused-argument
        userdata: dict,  # pylint: disable=unused-argument
        message: dict,
    ) -> None:
        """MQTT Function to handle status messages.

        Args:
        ----
            client: The client instance for this callback.
            userdata: The private user data as set in Client() or userdata_set().
            message: An instance of MQTTMessage.
        """
        data = json.loads(message.payload)
        # Send the data to the frontend
        socketio.emit(f"sensor_status_{data['client_id']}", data)
        socketio.emit("sensors_status_all", data)
        try:
            with app.app_context():
                sensor = Sensor.query.filter_by(
                    client_id=f"sensor-{data['client_id']}",
                ).first()
                if sensor:
                    # If the sensor already exists
                    sensor.ip_address = data["ip_address"]
                    sensor.max_distance = data.get("max_distance")
                    sensor.threshold = data["threshold"]
                    sensor.status = data.get("status")
                    sensor.last_update = datetime.now(pytz.timezone("Europe/Amsterdam"))

                    # Only update trigger_distance if status is "trigger"
                    if data["status"] == "trigger":
                        sensor.trigger_distance = data.get("distance", None)

                    db.session.commit()
                else:
                    # If the sensor doesn't exist
                    print(
                        f"Sensor: {data['client_id']}, doesn't exist in the database."
                    )
        except exc.IntegrityError:
            print("An error occurred during database operation.")
        except KeyError as error:
            print(f"MQTT data is missing the following key: {error}")

    # MQTT events
    mqtt.client.message_callback_add(MQTT_TRIGGER_TOPIC, on_topic_trigger)
    mqtt.client.message_callback_add(MQTT_STATUS_TOPIC, on_topic_status)


def register_routes(app: Flask) -> None:
    """Register the routes.

    Args:
    ----
        app: The Flask application.
    """

    @app.route("/set_color", methods=["GET"])
    def set_color() -> None:
        """Set LED strip color."""
        args = request.args
        led_controller.set_color(
            Color(int(args.get("red")), int(args.get("green")), int(args.get("blue"))),
        )
        return redirect(url_for("backend.led_control"))

    @app.route("/turn_off")
    def turn_off() -> None:
        """Turn off LED strip."""
        led_controller.color_wipe(Color(0, 0, 0), 10)
        return redirect(url_for("backend.led_control"))


# SocketIO events
@socketio.on("connect")
def on_connect() -> None:
    """SocketIO function to handle connect event."""
    print("Client connected")


@socketio.on("system_control")
def on_system_control(event: dict) -> None:
    """Put the system in active mode and start the workout.

    Args:
    ----
        event (dict): The event data.
    """
    control_workout(event)


@socketio.on("restart_sensors")
def on_restart_sensors(event: str) -> None:
    """SocketIO function to handle restart_sensors event.

    Args:
    ----
        event (str): The event data.
    """
    if event == "all_sensors":
        print("Restarting all sensors")
        mqtt.send(MQTT_RESTART_ALL_TOPIC, "restart")
    else:
        print(f"Restarting sensor - {event[7:]}")
        mqtt.send(f"{MQTT_SENSOR}/{event[7:]}/restart", "restart")
