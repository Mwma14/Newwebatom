from flask import Flask, render_template, request, jsonify, session, redirect, url_for
import asyncio
import threading
import time
from datetime import datetime
import database as db
from products import calculate_credit_cost
import config
from config import logger
import html
import random
import string

app = Flask(__name__)
app.secret_key = 'your-secret-key-change-this'

# Make config available in templates
@app.context_processor
def inject_config():
    return dict(config=config)

def run_async(coro):
    """Helper function to run async functions in sync context"""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()

@app.route('/')
def index():
    if 'user_id' in session:
        return redirect(url_for('dashboard'))
    return redirect(url_for('login'))

@app.route('/login', methods=['GET', 'POST'])
def login():
    if 'user_id' in session:
        return redirect(url_for('dashboard'))

    if request.method == 'POST':
        user_id = request.form.get('user_id')
        password = request.form.get('password')

        if not user_id:
            return render_template('login.html', error='Please enter your Telegram User ID')
        
        if not password:
            return render_template('login.html', error='Please enter your password')

        if len(password) != 6:
            return render_template('login.html', error='Password must be exactly 6 characters')

        try:
            user_id = int(user_id)
        except ValueError:
            return render_template('login.html', error='Invalid User ID format')

        # Check if user exists in database
        user_data = run_async(db.get_user_data(user_id))

        if user_data:
            # If user has no password set, redirect to set password page
            if not user_data.get('password'):
                session['temp_user_id'] = user_id
                return redirect(url_for('set_password'))
            else:
                # Verify existing password
                if run_async(db.verify_user_password(user_id, password)):
                    session['user_id'] = user_id
                    session['user_data'] = user_data
                    return redirect(url_for('dashboard'))
                else:
                    return render_template('login.html', error='Incorrect password')
        else:
            return render_template('login.html', error='User not found. Please start the bot first.')

    return render_template('login.html')

@app.route('/dashboard')
def dashboard():
    if 'user_id' not in session:
        return redirect(url_for('login'))

    user_id = session['user_id']
    # Always get fresh user data from database
    user_data = run_async(db.get_user_data(user_id))
    session['user_data'] = user_data

    # Get order history for dashboard
    pending, history = run_async(db.get_user_orders(user_id))
    recent_orders = (pending + history)[:5]

    formatted_recent_orders = []
    for order in recent_orders:
        o_id, pkg, status, ts_str, cost = order
        date_obj = datetime.fromisoformat(ts_str)
        formatted_recent_orders.append({
            'id': o_id,
            'package': pkg,
            'status': status.replace('_', ' ').title(),
            'cost': cost,
            'date': date_obj.strftime('%Y-%m-%d %H:%M')
        })

    return render_template('dashboard.html', 
                         user_data=user_data, 
                         user_id=user_id, 
                         recent_orders=formatted_recent_orders)

@app.route('/shop')
def shop():
    if 'user_id' not in session:
        return redirect(url_for('login'))

    # Always get fresh user data from database
    user_data = run_async(db.get_user_data(session['user_id']))
    session['user_data'] = user_data
    all_products = run_async(db.admin_get_all_products())
    operators = run_async(db.get_operators())

    organized_products = {}
    for operator_tuple in operators:
        operator = operator_tuple[0]
        organized_products[operator] = {}

        categories = run_async(db.get_categories_for_operator(operator))

        for category in categories:
            if category == "Beautiful Numbers":
                beautiful_numbers = run_async(db.get_beautiful_numbers(operator))
                if beautiful_numbers:
                    organized_products[operator][category] = {
                        'products': beautiful_numbers,
                        'type': 'bnum'
                    }
            else:
                products = run_async(db.get_products_in_category(operator, category))
                if products:
                    categorized = {'Beginner': [], 'Advanced': [], 'Expert': [], 'Professional': []}

                    for product in products:
                        prod_id, name, price_mmk, extra = product
                        credits = calculate_credit_cost(price_mmk)

                        product_info = {
                            'id': prod_id,
                            'name': name,
                            'price': price_mmk,
                            'credits': credits,
                            'extra': extra or ''
                        }

                        if credits <= 1:
                            categorized['Beginner'].append(product_info)
                        elif credits <= 3:
                            categorized['Advanced'].append(product_info)
                        elif credits <= 5:
                            categorized['Expert'].append(product_info)
                        else:
                            categorized['Professional'].append(product_info)

                    organized_products[operator][category] = {
                        'products': categorized,
                        'type': 'product'
                    }

    return render_template('shop.html', organized_products=organized_products, user_data=user_data)

@app.route('/credits')
def credits():
    if 'user_id' not in session:
        return redirect(url_for('login'))

    user_id = session['user_id']
    # Always get fresh user data from database
    user_data = run_async(db.get_user_data(user_id))
    session['user_data'] = user_data

    return render_template('credits.html', 
                         packages=config.CREDIT_PACKAGES,
                         user_data=user_data,
                         kbz_pay_number=config.KBZ_PAY_NUMBER,
                         wave_pay_number=config.WAVE_PAY_NUMBER,
                         credit_per_mmk=0.01)

@app.route('/purchase_credits', methods=['POST'])
def purchase_credits():
    if 'user_id' not in session:
        return jsonify({'success': False, 'error': 'Not logged in'})

    user_id = session['user_id']
    price_mmk = int(request.json.get('price_mmk'))
    payment_method = request.json.get('payment_method')

    try:
        credit_amount = calculate_credit_cost(price_mmk)
        order_id = f"WEB-CRD-{datetime.now().strftime('%y%m%d')}-{''.join(random.choices(string.ascii_uppercase + string.digits, k=4))}"

        # Create credit order with pending_payment status (same as bot)
        run_async(db.create_order(
            order_id=order_id,
            user_id=user_id,
            order_type='credit_purchase',
            package_name=f"{credit_amount:.2f} Credits",
            credit_cost=credit_amount,
            payment_method=payment_method,
            status='pending_payment'
        ))

        # Get payment number based on method
        payment_number = config.KBZ_PAY_NUMBER if payment_method == 'KBZ Pay' else config.WAVE_PAY_NUMBER

        return jsonify({
            'success': True,
            'order_id': order_id,
            'credit_amount': credit_amount,
            'price_mmk': price_mmk,
            'payment_method': payment_method,
            'payment_number': payment_number,
            'instructions': f"Send {price_mmk:,} MMK to {payment_number} using {payment_method}. You will receive {credit_amount:.2f} credits after verification."
        })

    except Exception as e:
        logger.error(f"Web credit purchase error: {e}")
        return jsonify({'success': False, 'error': str(e)})

@app.route('/upload_payment_screenshot', methods=['POST'])
def upload_payment_screenshot():
    if 'user_id' not in session:
        return jsonify({'success': False, 'error': 'Not logged in'})

    order_id = request.form.get('order_id')

    if 'screenshot' not in request.files:
        return jsonify({'success': False, 'error': 'No screenshot uploaded'})

    try:
        # Update order status to pending_approval (same as bot workflow)
        run_async(db.update_order_status_and_screenshot(order_id, 'pending_approval', 'web_upload'))

        # Send notification to @paymentrequestch channel (same as bot)
        user_id = session['user_id']
        user_data = run_async(db.get_user_data(user_id))

        def send_credit_notification():
            try:
                from telegram import InlineKeyboardMarkup, InlineKeyboardButton, Bot
                from telegram.constants import ParseMode

                # Get order details from database
                order_details = run_async(db.get_order_details(order_id))
                if order_details:
                    _, credit_cost, package_name, status, order_type = order_details

                    # Get payment method from the order
                    full_order = run_async(db.get_full_order_details(order_id))
                    payment_method = full_order[5] if full_order else "Unknown"  # payment_method column

                    user_username = f"(@{user_data.get('username', 'N/A')})" if user_data.get('username') else ""

                    # Format exactly like the bot does with payment method and price
                    price_mmk = int(credit_cost / 0.01)  # Convert credits back to MMK
                    admin_caption = (f"🚨 <b>New Credit Payment</b> 🚨\n\n"
                                   f"User: {html.escape(user_data.get('full_name', str(user_id)))} {user_username}\n"
                                   f"ID: <code>{user_id}</code>\n"
                                   f"Order ID: <code>{order_id}</code>\n"
                                   f"Amount: <b>{credit_cost:.2f} Credits</b>\n"
                                   f"Price: <b>{price_mmk:,} MMK</b>\n"
                                   f"💳 Method: <b>{payment_method}</b>\n"
                                   f"📱 Source: Web App")

                    admin_keyboard = InlineKeyboardMarkup([
                        [InlineKeyboardButton("✅ Approve", callback_data=f"admin_approve_credit_{order_id}_{user_id}"),
                         InlineKeyboardButton("❌ Reject", callback_data=f"admin_reject_credit_{order_id}_{user_id}")]])

                    async def send_async():
                        try:
                            bot = Bot(token=config.TOKEN)
                            # Send message with screenshot to the same channel as bot: @paymentrequestch
                            await bot.send_message(
                                config.CREDIT_REVIEW_CHANNEL, 
                                admin_caption + "\n\n📸 Payment screenshot received via web app", 
                                reply_markup=admin_keyboard, 
                                parse_mode=ParseMode.HTML
                            )
                            logger.info(f"✅ Credit payment notification sent to @paymentrequestch for order {order_id}")
                        except Exception as e:
                            logger.error(f"Failed to send to @paymentrequestch: {e}")

                    run_async(send_async())

            except Exception as e:
                logger.error(f"Failed to send web credit notification: {e}")

        threading.Thread(target=send_credit_notification).start()

        return jsonify({'success': True, 'message': 'Payment screenshot uploaded successfully. Your order is being reviewed.'})

    except Exception as e:
        logger.error(f"Screenshot upload error: {e}")
        return jsonify({'success': False, 'error': str(e)})

@app.route('/purchase_product', methods=['POST'])
def purchase_product():
    if 'user_id' not in session:
        return jsonify({'success': False, 'error': 'Not logged in'})

    user_id = session['user_id']
    product_id = request.json.get('product_id')
    phone_number = request.json.get('phone_number')

    try:
        user_data = run_async(db.get_user_data(user_id))

        # Check if it's a beautiful number or regular product
        bnum_details = run_async(db.get_bnum_by_id(product_id))
        if bnum_details:
            # Beautiful number
            _, phone, price_mmk, operator = bnum_details
            credit_cost = calculate_credit_cost(price_mmk)
            product_name = f"{operator} - {phone}"
        else:
            # Regular product
            product_details = run_async(db.get_product_by_id(product_id))
            if not product_details:
                return jsonify({'success': False, 'error': 'Product not found'})

            _, operator, category, name, price_mmk, _ = product_details
            credit_cost = calculate_credit_cost(price_mmk)
            product_name = name

        if user_data['credits'] < credit_cost:
            return jsonify({
                'success': False,
                'error': 'Insufficient credits! Please buy more credits.',
                'show_admin': True,
                'admin_telegram': '@CEO_METAVERSE',
                'admin_viber': '09883249943'
            })

        # Generate order ID
        order_id = f"WEB-{datetime.now().strftime('%y%m%d')}-{''.join(random.choices(string.ascii_uppercase + string.digits, k=4))}"

        # Deduct credits (same as bot)
        run_async(db.change_user_credits(user_id, -credit_cost))

        # Create order
        run_async(db.create_order(
            order_id=order_id,
            user_id=user_id,
            order_type='product_purchase',
            package_name=product_name,
            credit_cost=credit_cost,
            status='pending_approval',
            delivery_info=f"Web purchase - Phone: {phone_number}"
        ))

        # Send notification to @songbank12 channel (same as bot)
        def send_product_notification():
            try:
                from telegram import InlineKeyboardMarkup, InlineKeyboardButton, Bot
                from telegram.constants import ParseMode

                async def send_async():
                    try:
                        user_username = f"(@{user_data.get('username', 'N/A')})" if user_data.get('username') else ""

                        # Format exactly like the bot does
                        admin_caption = (f"📦 <b>New Product Order</b> 📦\n\n"
                                       f"User: {html.escape(user_data.get('full_name', str(user_id)))} {user_username}\n"
                                       f"ID: <code>{user_id}</code>\n"
                                       f"Order ID: <code>{order_id}</code>\n\n"
                                       f"Deliver: <code>{html.escape(product_name)}</code>\n"
                                       f"To: <code>{html.escape(phone_number)}</code>\n"
                                       f"💳 Source: Web App")

                        admin_keyboard = InlineKeyboardMarkup([[
                            InlineKeyboardButton("✅ Done", callback_data=f"admin_approve_product_{order_id}_{user_id}"),
                            InlineKeyboardButton("❌ Reject (Refund)", callback_data=f"admin_reject_product_{order_id}_{user_id}")
                        ]])

                        bot = Bot(token=config.TOKEN)
                        # Send to the same channel as bot: @songbank12
                        await bot.send_message(
                            config.ORDER_FULFILLMENT_CHANNEL,
                            admin_caption,
                            reply_markup=admin_keyboard,
                            parse_mode=ParseMode.HTML
                        )
                        logger.info(f"✅ Product order notification sent to @songbank12 for order {order_id}")
                    except Exception as e:
                        logger.error(f"Failed to send to @songbank12: {e}")

                run_async(send_async())

            except Exception as e:
                logger.error(f"Failed to send web product notification: {e}")

        threading.Thread(target=send_product_notification).start()

        return jsonify({
            'success': True,
            'order_id': order_id,
            'message': f'Product purchased successfully! Order ID: {order_id}'
        })

    except Exception as e:
        logger.error(f"Web product purchase error: {e}")
        return jsonify({'success': False, 'error': str(e)})

@app.route('/orders')
def orders():
    if 'user_id' not in session:
        return redirect(url_for('login'))

    user_id = session['user_id']
    pending, history = run_async(db.get_user_orders(user_id))

    formatted_pending = []
    formatted_history = []

    for order in pending:
        o_id, pkg, status, ts_str, cost = order
        date_obj = datetime.fromisoformat(ts_str)
        order_data = {
            'id': o_id,
            'package': pkg,
            'status': status.replace('_', ' ').title(),
            'cost': cost,
            'date': date_obj.strftime('%Y-%m-%d %H:%M'),
            'type': 'Credit Purchase' if 'Credit' in pkg else 'Product Purchase'
        }
        formatted_pending.append(order_data)

    for order in history:
        o_id, pkg, status, ts_str, cost = order
        date_obj = datetime.fromisoformat(ts_str)
        order_data = {
            'id': o_id,
            'package': pkg,
            'status': status.replace('_', ' ').title(),
            'cost': cost,
            'date': date_obj.strftime('%Y-%m-%d %H:%M'),
            'type': 'Credit Purchase' if 'Credit' in pkg else 'Product Purchase'
        }
        formatted_history.append(order_data)

    # Always get fresh user data from database
    user_data = run_async(db.get_user_data(user_id))
    session['user_data'] = user_data

    return render_template('orders.html', 
                         pending_orders=formatted_pending,
                         completed_orders=formatted_history,
                         user_data=user_data)

@app.route('/set_password', methods=['GET', 'POST'])
def set_password():
    if 'temp_user_id' not in session:
        return redirect(url_for('login'))
    
    if request.method == 'POST':
        password = request.form.get('password')
        confirm_password = request.form.get('confirm_password')
        
        if not password or not confirm_password:
            return render_template('set_password.html', error='Please fill in all fields')
        
        if len(password) != 6:
            return render_template('set_password.html', error='Password must be exactly 6 characters')
        
        if password != confirm_password:
            return render_template('set_password.html', error='Passwords do not match')
        
        user_id = session['temp_user_id']
        run_async(db.set_user_password(user_id, password))
        
        # Now log the user in
        user_data = run_async(db.get_user_data(user_id))
        session['user_id'] = user_id
        session['user_data'] = user_data
        session.pop('temp_user_id', None)
        
        return redirect(url_for('dashboard'))
    
    return render_template('set_password.html')

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('index'))

@app.route('/api/balance')
def get_balance():
    """API endpoint to get current user balance"""
    if 'user_id' not in session:
        return jsonify({'success': False, 'error': 'Not authenticated'}), 401

    try:
        user_data = run_async(db.get_user_data(session['user_id']))
        if user_data:
            # Update session with fresh data
            session['user_data'] = user_data
            return jsonify({
                'success': True, 
                'balance': user_data['credits'] or 0
            })
        else:
            return jsonify({'success': False, 'error': 'User not found'}), 404
    except Exception as e:
        logger.error(f"Error getting balance: {e}")
        return jsonify({'success': False, 'error': 'Internal server error'}), 500

@app.route('/admin')
def admin():
    if 'user_id' not in session:
        return redirect(url_for('login'))

    user_id = session['user_id']
    if user_id not in config.ADMIN_IDS:
        return redirect(url_for('dashboard'))

    return render_template('admin.html')

@app.route('/admin/orders')
def admin_orders():
    if 'user_id' not in session:
        return redirect(url_for('login'))

    if session['user_id'] not in config.ADMIN_IDS:
        return redirect(url_for('dashboard'))

    all_orders = run_async(db.admin_get_all_orders())

    formatted_orders = []
    for order in all_orders:
        o_id, u_id, pkg, cost, status, ts = order
        ts_formatted = datetime.fromisoformat(ts).strftime('%Y-%m-%d %H:%M')
        formatted_orders.append({
            'id': o_id,
            'user_id': u_id,
            'package': pkg,
            'cost': cost,
            'status': status.replace('_', ' ').title(),
            'timestamp': ts_formatted
        })

    return render_template('admin_orders.html', orders=formatted_orders)

@app.route('/admin/users')
def admin_users():
    if 'user_id' not in session:
        return redirect(url_for('login'))

    if session['user_id'] not in config.ADMIN_IDS:
        return redirect(url_for('dashboard'))

    # Get all user IDs and their basic info
    user_ids = run_async(db.admin_get_all_user_ids())
    users = []

    for (uid,) in user_ids[:50]:  # Limit to first 50 users for performance
        user_data = run_async(db.get_user_data(uid))
        users.append({
            'id': uid,
            'credits': user_data['credits'],
            'is_banned': user_data['is_banned']
        })

    return render_template('admin_users.html', users=users)

@app.route('/admin/products')
def admin_products():
    if 'user_id' not in session:
        return redirect(url_for('login'))

    if session['user_id'] not in config.ADMIN_IDS:
        return redirect(url_for('dashboard'))

    all_products = run_async(db.admin_get_all_products())

    formatted_products = []
    for product in all_products:
        operator, category, name, prod_id, price, active = product
        formatted_products.append({
            'id': prod_id,
            'operator': operator,
            'category': category,
            'name': name,
            'price': price,
            'active': active
        })

    return render_template('admin_products.html', products=formatted_products)

# Removed send_order_to_channel function - using direct channel notifications instead

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)