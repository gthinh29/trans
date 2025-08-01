"""Add source_url to Novel model

Revision ID: 7c9e4a02e58b
Revises: e088eef0933b
Create Date: 2025-07-19 13:29:04.042580

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '7c9e4a02e58b'
down_revision = 'e088eef0933b'
branch_labels = None
depends_on = None


def upgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    with op.batch_alter_table('novel', schema=None) as batch_op:
        batch_op.add_column(sa.Column('source_url', sa.String(length=512), nullable=True))

    # ### end Alembic commands ###


def downgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    with op.batch_alter_table('novel', schema=None) as batch_op:
        batch_op.drop_column('source_url')

    # ### end Alembic commands ###
